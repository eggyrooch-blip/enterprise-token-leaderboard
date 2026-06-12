#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书 AI 权益用量采集器(生产) —— Mac 端跑,CDP 驱动真实 Chrome 会话,上报给看板。

为什么这么设计(见 SPEC):飞书无官方用量 API;后台接口要 SPA 的 CSRF,纯 cookie 401;
页面注入触发防篡改。所以:连一个『拷贝你 Profile1 的 headless Chrome(带调试端口)』,
让页面自己鉴权,page.on('response') 在 CDP 层旁路抓响应(对防篡改不可见),归一化后
HTTPS 上报给看板,绝不直连 DB。每天跑的心跳让会话滚动续期;失效则告警,不静默写脏数据。

抓三类:
  - 额度盘   ai_center/homepage/ai_product_info → featureKeyQuotaMap{AI_credits,aily_credits}
  - 趋势     ai_center/overview/trend           → 按天按功能点数 + 人数
  - 全员逐人 ai_center/usage_detail/entity(POST,根部门=全员,offset 翻页)
身份:externalID = 飞连 user_id → email=user_id@<域名>,部门走飞连 department_path。

环境变量:
  FEISHU_CDP        必填,如 http://127.0.0.1:9223
  COLLECTOR_URL     看板上报地址,如 https://collector.example.com(默认 http://127.0.0.1:8000)
  COLLECTOR_TOKEN   Bearer token(对应 COLLECTOR_API_TOKENS 之一)
  FEISHU_EMAIL_DOMAIN  默认 example.com
  FEISHU_HOST        你的飞书企业域名,默认 your-tenant.feishu.cn
  FEILIAN_*         飞连凭证(选填;缺了就用飞书自带姓名/部门叶子)
  FEISHU_PRESET     日期预设,默认 近一月
  FEISHU_DRY_RUN    =1 只抓不报,打印归一化结果
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, date, timedelta

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)  # collector/
for cand in (os.path.join(HERE, ".env"), os.path.join(PARENT, ".env"),
             os.path.join(PARENT, "..", "pipeline", ".env")):
    if os.path.exists(cand):
        for _l in open(cand):
            _l = _l.strip()
            if _l and not _l.startswith("#") and "=" in _l:
                _k, _v = _l.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

CDP = os.environ.get("FEISHU_CDP", "http://127.0.0.1:9223")
COLLECTOR_URL = os.environ.get("COLLECTOR_URL", "http://127.0.0.1:8000").rstrip("/")
COLLECTOR_TOKEN = os.environ.get("COLLECTOR_TOKEN", "")
EMAIL_DOMAIN = os.environ.get("FEISHU_EMAIL_DOMAIN", "example.com")
PRESET = os.environ.get("FEISHU_PRESET", "近一月")
DRY = os.environ.get("FEISHU_DRY_RUN") == "1"
FEISHU_HOST = os.environ.get("FEISHU_HOST", "your-tenant.feishu.cn")  # 你的飞书企业域名
BASE = f"https://{FEISHU_HOST}/admin/aibilling"
PAGE_LIMIT = int(os.environ.get("FEISHU_PAGE_LIMIT", "100"))
# 逐日采集:每跑一次回采「最近 N 天」(含昨天),按天幂等覆盖 → 自动补前几天失败的缺口。
# 首次回填历史传大值(如 30);日常 launchd 跑默认小值即可。
BACKFILL_DAYS = int(os.environ.get("FEISHU_BACKFILL_DAYS", "7"))
CST = timezone(timedelta(hours=8))  # 飞书租户时区(Asia/Shanghai),单日窗口按它切


def log(*a):
    print(*a, flush=True)


def target_days():
    """要采的日期列表:昨天往前 BACKFILL_DAYS 天(升序)。不采今天(当天数据未结算完)。"""
    today = datetime.now(CST).date()
    return [today - timedelta(days=k) for k in range(BACKFILL_DAYS, 0, -1)]


def day_window(d):
    """某天 → (startTime, endTime) unix 秒,覆盖 00:00:00~23:59:59 (CST)。"""
    s = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=CST)
    e = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=CST)
    return int(s.timestamp()), int(e.timestamp())


# ---------------- 飞连身份(可选) ----------------
def load_feilian_map():
    """一次性拉飞连全员 → {user_id: {email,name,dept,avatar}}。缺凭证则返回 None。"""
    try:
        sys.path.insert(0, PARENT)
        from feilian_client import FeilianClient
        fc = FeilianClient()
        root = fc.root_department_id()
    except Exception as e:
        log(f"飞连不可用(用飞书自带身份兜底): {e}")
        return None
    # 同一 dict 双索引:user_id 键(ou_*) + email 键(含 @，与 uid 不冲突)。
    # user_id 命中不了的 aily 用户(externalID 不是飞连 user_id) → 按合成 email 兜底解析，
    # 拿到真实 department_path，不再落「裸组名→未归类」(孙可 2026-06-11)。
    # 分页循环也包在 try 内:飞连分页中途失败 → 保留已载部分(优雅降级)，绝不让采集崩
    # (codex 评审:飞连不可达→采集器照常落库，不崩)。
    m, offset, n = {}, 0, 0
    try:
        while True:
            data = fc._request("GET", "/api/open/v2/user/list",
                               query={"department_id": root, "fetch_child": "true",
                                      "limit": 100, "offset": offset})
            ul = (data or {}).get("user_list") or []
            for u in ul:
                rec = {"email": (u.get("email") or "").lower(),
                       "name": u.get("full_name") or "",
                       "dept": u.get("department_path") or "unknown",
                       "avatar": u.get("avatar") or ""}
                uid = u.get("user_id")
                if uid:
                    m[uid] = rec
                if rec["email"]:
                    m[rec["email"]] = rec   # email 兜底索引
                n += 1
            if len(ul) < 100:
                break
            offset += 100
    except Exception as e:
        log(f"飞连分页中断({n} 人已载，其余用飞书自带身份兜底): {e}")
        return m or None        # 有部分用部分，全无则 None → normalize 退回飞书裸名
    log(f"飞连身份预载 {n} 人")
    return m


# ---------------- 采集 ----------------
def collect():
    captured = {"detail": [], "single": {}}
    login_broken = {"v": False}
    req_tpl = {"headers": None, "body": None}

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()

        def on_resp(resp):
            u = resp.url
            if f"{FEISHU_HOST}/suite/admin/ai_center" not in u:
                return
            try:
                body = resp.json()
            except Exception:
                return
            if body.get("code") not in (0, None) or resp.status in (401, 403):
                login_broken["v"] = True
                return
            if "usage_detail/entity" in u:
                captured["detail"].append(body.get("data") or {})
            else:
                key = u.split("/ai_center/")[-1].split("?")[0]
                captured["single"][key] = body

        def on_req(req):
            # 抓真实 SPA 请求的整套头(含会话级 x-csrf-token)+ body 模板,供 API 翻页复用
            if "usage_detail/entity" in req.url and req.method == "POST":
                req_tpl["headers"] = dict(req.headers)
                req_tpl["body"] = req.post_data

        page.on("response", on_resp)
        page.on("request", on_req)

        # 1) 总览 → 额度 + 趋势 + 周期。轮询等关键响应到位(趋势/额度有时序抖动)。
        page.goto(f"{BASE}/usage-overview", wait_until="domcontentloaded")
        for _ in range(12):  # 最多等 ~12s
            page.wait_for_timeout(1000)
            if any(x in page.url for x in ("login", "passport", "accounts")):
                raise SystemExit("LOGIN_EXPIRED")
            if "overview/trend" in captured["single"] and "overview/feature" in captured["single"]:
                break

        # 2) 用量详情/成员 → 选根部门 → 预设 → 查询(暖会话 + 抓头 + 拿 total)
        page.goto(f"{BASE}/usage-detail?tab=member", wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
        page.get_by_text("请选择成员", exact=False).first.click(timeout=3000)
        page.wait_for_timeout(1200)
        node = page.get_by_text("Keep", exact=True).first
        node.locator("xpath=ancestor-or-self::*[self::li or @role='treeitem' or contains(@class,'item')][1]//input[@type='checkbox']").first.click(timeout=3000)
        page.wait_for_timeout(600)
        page.get_by_role("button", name="确定").last.click(timeout=2000)
        page.wait_for_timeout(800)
        page.get_by_placeholder("开始日期").first.click(timeout=2000)
        page.keyboard.type("2026-01-01", delay=20)
        page.wait_for_timeout(900)
        page.get_by_text(PRESET, exact=True).first.click(timeout=2000)
        page.wait_for_timeout(600)
        period = (page.get_by_placeholder("开始日期").first.input_value(timeout=1000),
                  page.get_by_placeholder("结束日期").first.input_value(timeout=1000))
        page.get_by_role("button", name="查询").first.click(timeout=2000)
        page.wait_for_timeout(4500)

        # 3) 全员逐人 —— 按天采:对每个目标日改写 startTime/endTime 单日窗口,offset 翻页拿全。
        #    单日窗口已实测可行(接口认窗口,返回当天增量,非累计)。按天落库 → 部门/个人榜可按
        #    区间聚合,不再是「一坨月累计」死快照;按天幂等覆盖自动补前几天失败的缺口。
        if login_broken["v"]:
            raise SystemExit("LOGIN_EXPIRED")
        captured["detail"] = []        # UI 暖会话那次(10/页,混窗口)丢掉,统一用 API 按天全量翻
        captured["detail_by_day"] = {}
        if not req_tpl["headers"] or not req_tpl["body"]:
            log("⚠️ 没抓到明细请求模板,无法按天采集")
        else:
            base = json.loads(req_tpl["body"])
            h = {k: v for k, v in req_tpl["headers"].items()
                 if k.lower() not in ("content-length", "host", ":authority", "accept-encoding", "connection")}
            base["limit"] = PAGE_LIMIT
            days = target_days()
            log(f"按天采集 {len(days)} 天:{days[0]} ~ {days[-1]}")
            for d in days:
                iso = d.isoformat()
                s, e = day_window(d)
                base["startTime"], base["endTime"] = s, e
                pages, off, total = [], 0, None
                while off < (total or 1):
                    base["offset"] = off
                    r = ctx.request.post(
                        f"https://{FEISHU_HOST}/suite/admin/ai_center/usage_detail/entity",
                        data=json.dumps(base), headers=h)
                    if r.status != 200:
                        log(f"  {iso} offset={off} HTTP {r.status},停止该天"); break
                    jd = r.json()
                    if jd.get("code") not in (0, None):
                        log(f"  {iso} offset={off} code={jd.get('code')},停止该天"); break
                    dd = jd.get("data") or {}
                    pages.append(dd)
                    total = int(dd.get("total") or 0) if total is None else total
                    got = len(dd.get("items", []))
                    off += PAGE_LIMIT
                    if got < PAGE_LIMIT:
                        break
                captured["detail_by_day"][iso] = pages
                n = sum(len(x.get("items", [])) for x in pages)
                log(f"  {iso}: {n} 人 (total={total})")

        # 4) 补抓每个功能的额度明细:overview/feature 在总览页只对默认功能(AI_credits)发了一次,
        #    aily 的 used/remain 缺 → 用 header-replay 按 featureKey 各拉一次(同会话 x-csrf-token 复用)。
        captured["feature_detail"] = {}
        info0 = (captured["single"].get("homepage/ai_product_info") or {}).get("data") or {}
        fkeys = list((info0.get("featureKeyQuotaMap") or {}).keys())
        if req_tpl.get("headers") and fkeys:
            hg = {k: v for k, v in req_tpl["headers"].items()
                  if k.lower() not in ("content-length", "host", ":authority",
                                       "accept-encoding", "connection", "content-type")}
            for fk in fkeys:
                try:
                    r = ctx.request.get(
                        f"https://{FEISHU_HOST}/suite/admin/ai_center/overview/feature?featureKey=" + fk,
                        headers=hg)
                    if r.status == 200:
                        jd = r.json()
                        if jd.get("code") in (0, None):
                            captured["feature_detail"][fk] = jd.get("data") or {}
                except Exception as e:
                    log("overview/feature %s 失败: %s" % (fk, str(e)[:50]))

        page.close()

    if login_broken["v"]:
        raise SystemExit("LOGIN_EXPIRED")
    return captured, period


# ---------------- 归一化 ----------------
def to_iso(s):
    """'2026/05/09' 或 '2026-05-09' → '2026-05-09'。"""
    s = (s or "").replace("/", "-")
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except Exception:
        return None


def normalize(captured, period, fmap):
    ps, pe = to_iso(period[0]), to_iso(period[1])
    out = {"period_start": ps, "period_end": pe, "members": [], "quota": [], "trend": []}

    # 额度:每功能真实 used/remain 来自 feature_detail(按 featureKey 各拉的 overview/feature);
    # 退路:总览页默认那次 overview/feature(只覆盖 AI_credits);再退路:总额度 + used=0。
    info = (captured["single"].get("homepage/ai_product_info") or {}).get("data") or {}
    qmap = info.get("featureKeyQuotaMap") or {}
    fdet = captured.get("feature_detail") or {}
    feat0 = (captured["single"].get("overview/feature") or {}).get("data") or {}
    for fk, total in qmap.items():
        try:
            total = float(total)
        except Exception:
            total = 0
        d = fdet.get(fk)
        if d:  # 该功能的真实明细(aily 用尽 → used=24万/remain=0 走这里)
            out["quota"].append({"feature_key": fk, "quota": float(d.get("quota") or total),
                                 "used": float(d.get("used") or 0), "remain": float(d.get("remain") or 0)})
        elif feat0 and float(feat0.get("quota") or 0) == total:
            out["quota"].append({"feature_key": fk, "quota": total,
                                 "used": float(feat0.get("used") or 0), "remain": float(feat0.get("remain") or 0)})
        else:
            out["quota"].append({"feature_key": fk, "quota": total, "used": 0, "remain": total})
    # 趋势(企业级,按天按 bizType):items{bizType: [{amount,dateTime}, ...]}。
    # bizTypeUserCount{bizType: 人数}是功能级总数(非按天),作为该 bizType 各天行的参考值。
    # bizConfigMap 是嵌套配置,拿不到干净的顶层功能名,biz_name 暂留空(后续可静态映射)。
    trend = (captured["single"].get("overview/trend") or {}).get("data") or {}
    items = trend.get("items") or {}
    ucount = trend.get("bizTypeUserCount") or {}
    if isinstance(items, dict):
        for bt, series in items.items():
            if isinstance(series, list):
                for pt in series:
                    iso = to_iso(pt.get("dateTime"))
                    if iso:
                        out["trend"].append({"usage_date": iso, "biz_type": str(bt),
                                             "biz_name": "",
                                             "credits": pt.get("amount") or 0,
                                             "user_count": int(ucount.get(str(bt)) or 0)})

    # 全员逐人(按天):detail_by_day = {usage_date: [data, ...]}。每天内按 uid 去重,
    # 每条 member 行带 usage_date → 看板按天落库、按区间聚合。
    by_day = captured.get("detail_by_day") or {}
    for iso, pages in by_day.items():
        seen = set()
        for d in pages:
            for it in d.get("items", []):
                ei = it.get("entityInfo") or {}
                uid = ei.get("externalID") or ""
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                ident = (fmap or {}).get(uid) or {}
                email = ident.get("email") or f"{uid}@{EMAIL_DOMAIN}"
                # user_id 没命中飞连 → 用合成 email 再查飞连(同 dict email 索引)，拿真实部门
                if not ident and fmap:
                    ident = fmap.get(email.lower()) or {}
                    if ident.get("name"):
                        email = ident.get("email") or email
                name = ident.get("name") or ei.get("entityName") or uid
                dept = ident.get("dept")
                if not dept or dept == "unknown":
                    dept = ((ei.get("entityExtraInfo") or {}).get("department") or {}).get("entityName") or "unknown"
                avatar = ident.get("avatar") or ei.get("avatarURL") or ""
                fm = it.get("featureUsageMap") or {}
                for fk, credits in fm.items():
                    out["members"].append({"email": email, "name": name, "dept": dept,
                                           "avatar": avatar, "entity_id": ei.get("entityID") or "",
                                           "feature_key": fk, "credits": credits or 0,
                                           "usage_date": iso})
    return out


# ---------------- 上报 ----------------
def report(payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{COLLECTOR_URL}/v1/feishu/report", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {COLLECTOR_TOKEN}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main():
    fmap = load_feilian_map()
    try:
        captured, period = collect()
    except SystemExit as e:
        if str(e) == "LOGIN_EXPIRED":
            log("❌ LOGIN_EXPIRED:登录态失效,不写脏数据。请刷新 auto_udd 的登录态(见 refresh_profile)。")
            return 3
        raise
    payload = normalize(captured, period, fmap)
    days = sorted({m.get("usage_date") for m in payload["members"] if m.get("usage_date")})
    log(f"归一化:按天 {len(days)} 天 {days[0] if days else '-'}~{days[-1] if days else '-'} | "
        f"全员 {len({m['email'] for m in payload['members']})} 人 / {len(payload['members'])} 行 | "
        f"额度 {len(payload['quota'])} | 趋势 {len(payload['trend'])} 行")
    if DRY:
        log(json.dumps({k: (v[:2] if isinstance(v, list) else v) for k, v in payload.items()},
                       ensure_ascii=False, indent=2)[:1500])
        return 0
    if not COLLECTOR_TOKEN:
        log("⚠️ 无 COLLECTOR_TOKEN,跳过上报(等同 dry-run)"); return 0
    res = report(payload)
    log("✅ 上报:", res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
