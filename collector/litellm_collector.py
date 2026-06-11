#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 LiteLLM 网关用量周期性灌进收集端 SQLite —— 个人 key 进个人榜, agent key 进单独 agent 榜.

为什么是这一份(而不是旧 litellm_sync.py):
  - 旧版写 Postgres(asyncpg/httpx), 但线上收集端是 dev_collector.py(SQLite). 表/依赖都对不上.
  - 旧版没有 agent / 个人 拆分.
本模块与 dev_collector.py 同宿主(collector.example.com, Python 3.6.8, 纯标准库), 直接写同一个 tok.db,
免 HTTP 自上报, 也就绕开了 /v1/tokscale/report 把 source 硬编码成 'subscription' 的限制.

数据来源(只读 LiteLLM 管理 API, master key):
  GET /team/list                 → 找到 alias='agent' 的 team_id(区分 agent / 个人 的唯一依据)
  GET /key/list?return_full_object=true  → token_hash → {user_id, team_id, key_alias}
  GET /user/list                 → user_id → email(个人归属)
  GET /user/daily/activity       → 每天 / 每模型 / 每 key 的 token 明细(api_key_breakdown)

归属规则:
  team_id == AGENT_TEAM_ID  → agent: email='agent:'+key_alias, dept='agent', source='litellm_agent'
  否则                       → 个人: email=该 user 的企业邮箱,   dept=team_alias, source='litellm'

幂等: 每次跑都拉 [HISTORY_START, today] 全窗口, 重算 day / lifetime / month 三类桶后
INSERT OR REPLACE 覆盖写. 连跑两次总量不变(不翻倍).

环境变量:
  LITELLM_BASE_URL          (必填)  e.g. https://litellm.example.com
  LITELLM_MASTER_KEY        (必填)  sk-...
  LITELLM_AGENT_TEAM_ALIAS  默认 'agent'
  LITELLM_AGENT_TEAM_ID     可选, 兜底/覆盖(默认空,优先按 alias 自动解析)
  LITELLM_HISTORY_START     默认 '2025-01-01'(lifetime/month 聚合窗口起点)
  LITELLM_CLIENT_LABEL      默认 'LiteLLM'(写进 usage.client, 给工具榜用)
  DEV_DB                    默认 '/tmp/tok.db'(线上 timer 注入 /home/it/tokreport/tok.db)
  HTTP_TIMEOUT              默认 '60'

用法:
  python3 litellm_collector.py            # 真写库
  python3 litellm_collector.py --dry-run  # 只打印归属汇总, 不写库(可对生产只读演练)
"""
from __future__ import print_function

import datetime
import json
import os
import sqlite3
import sys
from collections import defaultdict

try:                                  # Py3
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode
    from urllib.error import HTTPError, URLError
except ImportError:                   # 理论上不会走到(目标是 py3.6), 保底
    from urllib2 import Request, urlopen, HTTPError, URLError  # type: ignore
    from urllib import urlencode       # type: ignore


# --------------------------------------------------------------------------- 配置
BASE = os.environ.get("LITELLM_BASE_URL", "").rstrip("/")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
AGENT_TEAM_ALIAS = os.environ.get("LITELLM_AGENT_TEAM_ALIAS", "agent")
AGENT_TEAM_ID_FALLBACK = os.environ.get("LITELLM_AGENT_TEAM_ID", "")
# 白名单:这些 key_alias 虽无过期(会被误判为 agent),实为「个人消耗」,
# 强制归到其 owner 的个人榜、不进 agent 榜。逗号分隔,可经环境变量扩充。
PERSONAL_KEY_ALIASES = set(
    a.strip() for a in os.environ.get(
        "LITELLM_PERSONAL_KEY_ALIASES", "cursor,zhaobo03_coding").split(",")
    if a.strip())
# 归属覆盖表:个别 key 在 LiteLLM 里既无 user_id 也无 created_by(裸 admin/探针 key),
# 但运营上确知归属人时,在此手工钉死 alias→邮箱。格式 "alias1:a@x.com,alias2:b@x.com"。
# 兜底优先级:user_id → created_by → 本覆盖表 → 合成 litellm-key:<alias>。
KEY_OWNER_OVERRIDES = {}
for _pair in os.environ.get("LITELLM_KEY_OWNER_MAP", "").split(","):
    if ":" in _pair:
        _a, _e = _pair.split(":", 1)
        if _a.strip() and _e.strip():
            KEY_OWNER_OVERRIDES[_a.strip()] = _e.strip()
# 探针/测试 key 别名前缀:命中即从所有榜单剔除(无真人 owner 的调试噪音)。逗号分隔。
PROBE_ALIAS_PREFIXES = tuple(
    p.strip() for p in os.environ.get("LITELLM_PROBE_ALIAS_PREFIXES", "tmp-").split(",")
    if p.strip())
# 精确匹配的探针别名:已删、查不到任何真人 owner 的服务号/短别名垃圾 key(逗号分隔,整名相等才剔除)。
# 区别于 PROBE_ALIAS_PREFIXES(前缀匹配);精确匹配避免误伤(如 'ss' 不会连累 'ss-platform')。
PROBE_ALIASES = set(
    a.strip() for a in os.environ.get("LITELLM_PROBE_ALIASES", "").split(",") if a.strip())
# 身份合并:把同一人的分身/外部邮箱归并到规范真人邮箱。与 cursor_sync 共享同一 helper(同表同逻辑)。
# 惰性读 env,故 import 顺序无所谓。值含真实员工邮箱,只在生产 env 配置(LITELLM_EMAIL_MERGE_MAP)。
from email_merge import merge_email  # noqa: E402  (同目录共享模块,运行目录在 sys.path)
HISTORY_START = os.environ.get("LITELLM_HISTORY_START", "2025-01-01")
CLIENT_LABEL = os.environ.get("LITELLM_CLIENT_LABEL", "LiteLLM")
DB = os.environ.get("DEV_DB", "/tmp/tok.db")
TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "60"))

DRY_RUN = "--dry-run" in sys.argv


def _die(msg):
    sys.stderr.write("litellm_collector: " + msg + "\n")
    sys.exit(2)


# --------------------------------------------------------------------------- HTTP
def _get(path, params=None):
    """GET <BASE><path>?<params>, Bearer master key, 返回解析后的 JSON.失败抛异常."""
    url = BASE + path
    if params:
        url += "?" + urlencode(params)
    req = Request(url, headers={"Authorization": "Bearer " + MASTER_KEY,
                                "Accept": "application/json"})
    resp = urlopen(req, timeout=TIMEOUT)
    raw = resp.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw or "{}")


# --------------------------------------------------------------------------- 拉取
def resolve_agent_team_id():
    """/team/list 里找 alias==AGENT_TEAM_ALIAS 的 team_id; 同时返回 team_id→alias 映射(给 dept).

    返回 (agent_team_id, {team_id: team_alias}).找不到 alias 时回退到 AGENT_TEAM_ID_FALLBACK.
    """
    data = _get("/team/list")
    teams = data if isinstance(data, list) else (data.get("teams") or [])
    alias_by_id = {}
    agent_id = None
    for t in teams:
        if not isinstance(t, dict):
            continue
        tid = t.get("team_id")
        alias = t.get("team_alias") or ""
        if tid:
            alias_by_id[tid] = alias
        if alias == AGENT_TEAM_ALIAS:
            agent_id = tid
    return (agent_id or AGENT_TEAM_ID_FALLBACK), alias_by_id


def fetch_key_map():
    """分页拉 /key/list, 返回 token_hash → {user_id, team_id, key_alias}."""
    out = {}
    page = 1
    size = 100
    while True:
        data = _get("/key/list", {"return_full_object": "true",
                                  "page": page, "size": size})
        keys = data.get("keys") if isinstance(data, dict) else None
        if keys is None:
            keys = data.get("data") if isinstance(data, dict) else None
        if keys is None:
            keys = data if isinstance(data, list) else []
        for k in keys:
            if isinstance(k, str):          # 偶尔只返回 token 字符串, 无 meta 可用
                out.setdefault(k, {})
                continue
            tok = k.get("token") or k.get("key")
            if not tok:
                continue
            out[tok] = {
                "user_id": k.get("user_id"),
                "team_id": k.get("team_id"),
                "key_alias": k.get("key_alias") or (k.get("key_name") or tok[:8]),
                "user_email": k.get("user_email"),  # 部分 key 直接带, 作 /user/list 的兜底
                "created_by": k.get("created_by"),   # 创建该 key 的 user_id, 无 owner 时的归属兜底
                "expires": k.get("expires"),         # agent 判定依据: 无过期(None/空)=agent
            }
        total = (data.get("total_count") if isinstance(data, dict) else None) or 0
        if len(keys) < size or (total and len(out) >= total) or page > 50:
            break
        page += 1
    return out


def fetch_users():
    """分页拉 /user/list, 返回 user_id → {"email", "name"}.

    注意: LiteLLM 的 /user/list 在 page_size>100 时返回空数组(实测), 故固定 100 分页.
    user_alias 是中文姓名(如 '李相锟'), 顺手带出来改善看板显示.
    """
    out = {}
    page = 1
    size = 100  # >100 会返回空, 别改大
    while True:
        try:
            data = _get("/user/list", {"page": page, "page_size": size})
        except (HTTPError, URLError):
            break
        users = data.get("users") if isinstance(data, dict) else None
        if users is None:
            users = data if isinstance(data, list) else []
        for u in users:
            if not isinstance(u, dict):
                continue
            uid = u.get("user_id")
            if not uid:
                continue
            out[uid] = {
                "email": u.get("user_email") or u.get("email"),
                "name": u.get("user_alias") or "",
            }
        total_pages = data.get("total_pages") if isinstance(data, dict) else None
        if len(users) < size or (total_pages and page >= total_pages) or page > 100:
            break
        page += 1
    return out


def fetch_daily_activity():
    """分页拉 /user/daily/activity 全窗口, 返回 results 列表(每天一个全局聚合 + key 明细)."""
    today = datetime.date.today().isoformat()
    results = []
    page = 1
    size = 1000
    while True:
        data = _get("/user/daily/activity",
                    {"start_date": HISTORY_START, "end_date": today,
                     "page": page, "page_size": size})
        rs = (data.get("results") if isinstance(data, dict) else None) or []
        results.extend(rs)
        meta = (data.get("metadata") if isinstance(data, dict) else None) or {}
        total_pages = meta.get("total_pages")
        # 翻页判停 —— 该端点每页只回少量「天聚合」条目(远小于 page_size),
        # 绝不能用 len(rs) < size 判停, 否则第 1 页就 break, 只吃到最近 1-2 天(老数据全丢)。
        if not rs:
            break                                          # 空页 = 翻完
        if total_pages is not None:
            if page >= total_pages:
                break                                      # 有总页数: 翻到末页停
        elif page >= 200:
            break                                          # 无总页数(异常端点)的安全上限
        page += 1
    return results


# --------------------------------------------------------------------------- 聚合
def _num(d, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                try:
                    return int(float(v))
                except (TypeError, ValueError):
                    pass
    return 0


class Bucket(object):
    """一个 (email,dept,source,client,provider,model) 维度的可累加桶."""
    __slots__ = ("email", "dept", "source", "client", "provider", "model",
                 "inp", "out", "cr", "cw", "rs", "cost", "msg")

    def __init__(self, email, dept, source, client, provider, model):
        self.email = email; self.dept = dept; self.source = source
        self.client = client; self.provider = provider; self.model = model
        self.inp = self.out = self.cr = self.cw = self.rs = 0
        self.cost = 0.0; self.msg = 0

    def add(self, inp, out, cr, cw, rs, cost, msg):
        self.inp += inp; self.out += out; self.cr += cr; self.cw += cw
        self.rs += rs; self.cost += cost; self.msg += msg

    @property
    def total(self):
        return self.inp + self.out + self.cr + self.cw + self.rs


def build_rows(results, key_map, users, agent_team_id, alias_by_id):
    """把 daily/activity 摊平成三类周期桶(day / lifetime / month).

    返回 (rows, stats):
      rows  = [(email,dept,period_type,period,source,client,provider,model,
                input,output,cache_read,cache_write,reasoning,total,cost,messages,
                name, is_agent), ...]   —— 末两列给 people 落档用, 不入 usage.
      stats = {personal_keys, agent_keys, unknown_keys, days, ...}
    """
    day = {}        # (key-of-bucket, date)   -> Bucket
    life = {}       # key-of-bucket           -> Bucket
    month = {}      # (key-of-bucket, YYYY-MM)-> Bucket
    names = {}      # email -> (name, is_agent)
    agent_owner = {}  # 'agent:<alias>' -> (owner_email, owner_name)  归属人
    seen_personal = set(); seen_agent = set(); unknown = set()
    probe_skipped = set()
    days = set()
    # 规范邮箱 → 真人记录(取合并目标的中文名)
    users_by_email = {(u.get("email") or "").lower(): u
                      for u in users.values() if u.get("email")}

    def bkey(b):
        return (b.email, b.source, b.client, b.provider, b.model)

    for entry in results:
        date = entry.get("date")
        if not date:
            continue
        days.add(date)
        ym = date[:7]
        models = ((entry.get("breakdown") or {}).get("models")) or {}
        for model, mv in models.items():
            akb = (mv.get("api_key_breakdown") or {}) if isinstance(mv, dict) else {}
            for tok, kv in akb.items():
                m = (kv.get("metrics") or {}) if isinstance(kv, dict) else {}
                inp = _num(m, "prompt_tokens")
                out = _num(m, "completion_tokens")
                cr = _num(m, "cache_read_input_tokens")
                cw = _num(m, "cache_creation_input_tokens")
                rs = _num(m, "reasoning_tokens")
                cost = float(m.get("spend") or 0.0)
                msg = _num(m, "successful_requests")
                if (inp + out + cr + cw + rs) == 0 and cost == 0.0:
                    continue  # 全 0(通常是 failed_requests 行), 不占行

                meta = key_map.get(tok)
                from_keylist = meta is not None
                # daily/activity 自带 metadata 兜底(线上 key 被删时 key_map 可能没有)
                if not meta:
                    kmd = (kv.get("metadata") or {})
                    meta = {"user_id": None, "team_id": kmd.get("team_id"),
                            "key_alias": kmd.get("key_alias") or tok[:8],
                            "expires": "unknown"}
                    if not kmd:
                        unknown.add(tok)
                team_id = meta.get("team_id")
                alias = meta.get("key_alias") or tok[:8]

                # 探针/测试 key 过滤: tmp-* 这类是调试时打的一次性 key(master key 创建、无真人
                # owner、用完即删),只在历史 activity 留下 token 噪音。直接跳过,不进任何榜、不进未归类。
                if PROBE_ALIAS_PREFIXES and any(alias.startswith(pfx) for pfx in PROBE_ALIAS_PREFIXES):
                    probe_skipped.add(alias)
                    continue
                # 精确匹配的垃圾别名(服务号/短别名,已删且查无真人 owner)。
                if alias in PROBE_ALIASES:
                    probe_skipped.add(alias)
                    continue
                # 同类无别名孤儿: 已从 /key/list 删除、当初连 key_alias 都没设(alias 退化成 token
                # 前缀)、且无任何 owner —— 裸 /key/generate 调试 key, 不可能是已入职员工, 一并剔除。
                if (not from_keylist and alias == tok[:8]
                        and not meta.get("user_id") and not meta.get("user_email")):
                    probe_skipped.add(alias)
                    continue

                # agent 判定(2026-06-08 修正): agent key 的特征是「无过期时间」。
                # 只有 /key/list 里确实存在、且 expires 为空的 key 才算 agent;
                # 未知 key(可能已删)一律按个人, 绝不污染 agent 榜。
                is_agent = from_keylist and not meta.get("expires")
                # 白名单 key(如个人 cursor / zhaobo03_coding):虽无过期但是个人消耗,
                # 强制走个人分支 → 归到 owner 个人榜,不污染 agent 榜。
                if is_agent and alias in PERSONAL_KEY_ALIASES:
                    is_agent = False
                if is_agent:
                    email = "agent:" + alias
                    dept = "agent"
                    source = "litellm_agent"
                    names[email] = (alias, True)
                    # 归属人: agent key 的 owner = 创建它的 litellm user
                    uid = meta.get("user_id")
                    urec = users.get(uid) if uid else None
                    owner_email = (urec or {}).get("email") or meta.get("user_email") or ""
                    owner_name = (urec or {}).get("name") or (
                        owner_email.split("@")[0] if owner_email else "")
                    agent_owner[email] = (owner_email, owner_name)
                    seen_agent.add(email)
                else:
                    uid = meta.get("user_id")
                    urec = users.get(uid) if uid else None
                    email = (urec or {}).get("email") or meta.get("user_email")
                    pname = (urec or {}).get("name") or ""
                    # 归属兜底①: key 自身无 owner 时, 用「创建该 key 的 user」(created_by)→ users 表
                    if not email:
                        cb = meta.get("created_by")
                        cbrec = users.get(cb) if cb else None
                        if cbrec and cbrec.get("email"):
                            email = cbrec["email"]
                            pname = pname or cbrec.get("name") or ""
                    # 归属兜底②: 运营手工钉死的 alias→邮箱 覆盖表
                    if not email and alias in KEY_OWNER_OVERRIDES:
                        email = KEY_OWNER_OVERRIDES[alias]
                    # 都没有 → 合成身份(裸 key/探针, 确无真人 owner)
                    if not email:
                        email = ("litellm-user:" + uid) if uid else ("litellm-key:" + alias)
                    # 身份合并: 分身/外部邮箱归并到规范真人邮箱, 用目标的中文名(不带分身旧名)
                    merged = merge_email(email)
                    if merged != email:
                        email = merged
                        pname = users_by_email.get(merged.lower(), {}).get("name") or ""
                    pname = pname or email.split("@")[0]
                    dept = alias_by_id.get(team_id) or "unknown"
                    source = "litellm"
                    names.setdefault(email, (pname, False))
                    seen_personal.add(email)

                b_template = Bucket(email, dept, source, CLIENT_LABEL, "", model)
                k = bkey(b_template)

                db = day.get((k, date))
                if db is None:
                    db = Bucket(email, dept, source, CLIENT_LABEL, "", model)
                    day[(k, date)] = db
                db.add(inp, out, cr, cw, rs, cost, msg)

                lb = life.get(k)
                if lb is None:
                    lb = Bucket(email, dept, source, CLIENT_LABEL, "", model)
                    life[k] = lb
                lb.add(inp, out, cr, cw, rs, cost, msg)

                mb = month.get((k, ym))
                if mb is None:
                    mb = Bucket(email, dept, source, CLIENT_LABEL, "", model)
                    month[(k, ym)] = mb
                mb.add(inp, out, cr, cw, rs, cost, msg)

    rows = []

    def emit(b, period_type, period):
        name, is_agent = names.get(b.email, (b.email.split(":")[-1], False))
        rows.append((
            b.email, b.dept, period_type, period, b.source, b.client, b.provider, b.model,
            b.inp, b.out, b.cr, b.cw, b.rs, b.total, round(b.cost, 6), b.msg,
            name, is_agent,
        ))

    for (k, date), b in day.items():
        emit(b, "day", date)
    for k, b in life.items():
        emit(b, "lifetime", "all")
    for (k, ym), b in month.items():
        emit(b, "month", ym)

    stats = {
        "personal_identities": len(seen_personal),
        "agent_identities": len(seen_agent),
        "unknown_keys": len(unknown),
        "probe_skipped": len(probe_skipped),
        "days": len(days),
        "rows": len(rows),
    }
    return rows, names, agent_owner, stats


# --------------------------------------------------------------------------- 写库
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS usage(
    email TEXT NOT NULL, dept TEXT NOT NULL DEFAULT '',
    period_type TEXT NOT NULL, period TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'subscription',
    client TEXT NOT NULL DEFAULT 'unknown', provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT 'unknown',
    input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0, cache_write INTEGER NOT NULL DEFAULT 0,
    reasoning INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (email, period_type, period, source, client, provider, model))
"""
_PEOPLE_TABLE = """
CREATE TABLE IF NOT EXISTS people(
    email TEXT PRIMARY KEY, name TEXT, avatar TEXT, dept TEXT)
"""
_UPSERT = """
INSERT OR REPLACE INTO usage
    (email, dept, period_type, period, source, client, provider, model,
     input, output, cache_read, cache_write, reasoning, total, cost, messages)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def _feilian():
    """尝试用同目录 feilian_client + .env 凭证拿一个飞连客户端; 失败返回 None(头像兜底是可选的)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from feilian_client import FeilianClient
        return FeilianClient()
    except Exception:
        return None


_FEILIAN_AVATARS = os.environ.get("LITELLM_FEILIAN_AVATARS", "1") not in ("0", "false", "")
_FEILIAN_MAX = int(os.environ.get("LITELLM_FEILIAN_MAX", "300"))  # 单次跑最多查多少个飞连邮箱


def _resolve_feilian_info(conn, emails):
    """email → (中文名, 头像, 部门全路径). 先 join 现有 people, 仍缺头像或缺 Keep 部门的
    再走飞连 user_by_email 兜底(只查缺的, 查到落库后下次自然跳过). 返回 {email:(name,avatar,dept)}.
    历史 bug:旧版只取 name+avatar，丢了同一返回里的 department_path → 纯 API 用户 dept 永远空、
    落未归类。这里把 dept 一并带回，write_db 用它补空 dept(自愈，litellm-sync.timer 每小时跑)。"""
    info = {}
    for e in emails:
        row = conn.execute(
            "SELECT name, avatar, dept FROM people WHERE email=?", (e,)).fetchone()
        if row and (row[0] or row[1] or row[2]):
            info[e] = (row[0] or "", row[1] or "", row[2] or "")

    def _needs(e):
        n, a, d = info.get(e, ("", "", ""))
        return (not a) or (not str(d).startswith("Keep"))  # 缺头像 或 缺真实(Keep)部门

    missing = [e for e in emails if _needs(e)]
    if missing and _FEILIAN_AVATARS:
        fc = _feilian()
        root = None
        if fc:
            try:
                root = fc.root_department_id()
            except Exception:
                root = None
        if fc and root:
            for e in missing[:_FEILIAN_MAX]:
                try:
                    u = fc.user_by_email(e, root)
                except Exception:
                    u = None
                if u:
                    prev = info.get(e, ("", "", ""))
                    info[e] = (u.get("full_name") or prev[0],
                               u.get("avatar") or prev[1],
                               u.get("department_path") or prev[2])
    return info


def write_db(rows, names, agent_owner):
    conn = sqlite3.connect(DB)
    try:
        conn.execute(_CREATE_TABLE)
        conn.execute(_PEOPLE_TABLE)
        # 先清掉本来源的旧 litellm 行, 再整批重写 —— 避免“窗口内某 model 不再出现”留下陈旧行.
        conn.execute("DELETE FROM usage WHERE source IN ('litellm','litellm_agent')")
        for r in rows:
            conn.execute(_UPSERT, r[:16])
        # 需要头像的 email: agent 归属人 + 个人榜本人, 一次性解析(只查缺头像的)
        owner_emails = set(oe for (oe, _n) in agent_owner.values() if oe)
        personal_emails = set(e for e, (_nm, ia) in names.items() if not ia and "@" in e)
        finfo = _resolve_feilian_info(conn, owner_emails | personal_emails)
        # people 落档:
        #   agent → name=alias, avatar=归属人头像, dept=归属人中文名(看板显示“隶属 X”+头像)
        #   个人  → 不覆盖 tokscale 已落的中文名/头像; 仅给缺头像的补飞连头像
        for email, (name, is_agent) in names.items():
            if is_agent:
                oe, on = agent_owner.get(email, ("", ""))
                oname, oavatar, _od = finfo.get(oe, ("", "", ""))
                owner_disp = oname or on or (oe.split("@")[0] if oe else "")
                # agent → dept 槽位放归属人中文名(看板显示“隶属 X”)，不写飞连部门
                conn.execute(
                    "INSERT OR REPLACE INTO people(email,name,avatar,dept) VALUES(?,?,?,?)",
                    (email, name, oavatar or "", owner_disp))
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO people(email,name,avatar,dept) VALUES(?,?,?,?)",
                    (email, name, "", ""))
                fn, fav, fdept = finfo.get(email, ("", "", ""))
                # 补头像(缺才补)
                if fav:
                    conn.execute(
                        "UPDATE people SET avatar=?, "
                        "name=CASE WHEN name IS NULL OR name='' THEN ? ELSE name END "
                        "WHERE email=? AND (avatar IS NULL OR avatar='')",
                        (fav, fn or name, email))
                # 补部门(历史 bug 修复):dept 为空/非 Keep 且飞连查到真实部门 → 填上，不覆盖已有 Keep 部门
                if fdept and str(fdept).startswith("Keep"):
                    conn.execute(
                        "UPDATE people SET dept=? "
                        "WHERE email=? AND (dept IS NULL OR dept='' OR dept NOT LIKE 'Keep%')",
                        (fdept, email))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- 入口
def main():
    if not BASE or not MASTER_KEY:
        _die("缺少 LITELLM_BASE_URL / LITELLM_MASTER_KEY 环境变量")

    agent_team_id, alias_by_id = resolve_agent_team_id()
    key_map = fetch_key_map()
    users = fetch_users()
    results = fetch_daily_activity()
    rows, names, agent_owner, stats = build_rows(
        results, key_map, users, agent_team_id, alias_by_id)

    sys.stderr.write(
        "litellm_collector: agent_team={atid} keys={nk} users={nu} days={d} "
        "→ personal={p} agents={a} unknown_keys={u} rows={r}\n".format(
            atid=agent_team_id, nk=len(key_map), nu=len(users),
            d=stats["days"], p=stats["personal_identities"],
            a=stats["agent_identities"], u=stats["unknown_keys"], r=stats["rows"]))

    if DRY_RUN:
        # 打印 lifetime top 个人 + 全部 agent, 供只读演练核对
        life = [r for r in rows if r[2] == "lifetime"]
        agg = defaultdict(lambda: [0, 0.0])  # email -> [tokens, cost]
        is_agent = {}
        for r in life:
            agg[r[0]][0] += r[13]; agg[r[0]][1] += r[14]
            is_agent[r[0]] = r[17]
        ppl = sorted([(e, v) for e, v in agg.items() if not is_agent[e]],
                     key=lambda x: -x[1][0])
        ags = sorted([(e, v) for e, v in agg.items() if is_agent[e]],
                     key=lambda x: -x[1][0])
        print("\n--- DRY RUN (未写库) ---")
        print("[个人榜 top10 / source=litellm]")
        for e, v in ppl[:10]:
            print("  {:>14,} tok  ${:>9.2f}  {}".format(v[0], v[1], e))
        print("[agent 榜 / source=litellm_agent]  共 {}".format(len(ags)))
        for e, v in ags[:20]:
            print("  {:>14,} tok  ${:>9.2f}  {}".format(v[0], v[1], e))
        print("(DEV_DB={} 未改动)".format(DB))
        return

    write_db(rows, names, agent_owner)
    sys.stderr.write("litellm_collector: wrote {} rows to {}\n".format(len(rows), DB))


if __name__ == "__main__":
    main()
