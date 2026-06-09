#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cursor 维度接入(企业级,无感知)—— 取按天 token,可与 Claude/Codex 求和。

数据源(Cursor 团队 Admin API,Basic 鉴权):
  - /teams/members            成员(email→中文名兜底)
  - /teams/filtered-usage-events  事件级用量(含 tokenUsage + model + 时间) ← 真 token
分页拉事件 → 按 (email, 天, 模型) 聚合 token/cost → 落:
  - 日桶  period_type='day'      (支持区间榜,与 CLI 日桶同表求和)
  - 全部  period_type='lifetime' (窗口内累计,供"全部"维度)
身份经飞连按 email 反查 中文姓名/部门/头像 → people 表。

环境:CURSOR_API_KEY(必填)、FEILIAN_*(选填)、DEV_DB、CURSOR_WINDOW_DAYS(默认30)。
用法:python3 cursor_sync.py [tok.db]
"""
import base64
import datetime
import json
import os
import sqlite3
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, ".env"), os.path.join(HERE, "..", "pipeline", ".env")):
    if os.path.exists(cand):
        for _l in open(cand):
            _l = _l.strip()
            if _l and not _l.startswith("#") and "=" in _l:
                _k, _v = _l.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

DB = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DEV_DB", "tok.db")
KEY = os.environ["CURSOR_API_KEY"]
BASE = os.environ.get("CURSOR_API_BASE", "https://api.cursor.com").rstrip("/")
WINDOW_DAYS = int(os.environ.get("CURSOR_WINDOW_DAYS", "30"))
PAGE_SIZE = int(os.environ.get("CURSOR_PAGE_SIZE", "1000"))
MAX_PAGES = int(os.environ.get("CURSOR_MAX_PAGES", "200"))
_AUTH = "Basic " + base64.b64encode((KEY + ":").encode()).decode()

_UPSERT = """INSERT OR REPLACE INTO usage
 (email,dept,period_type,period,source,client,provider,model,
  input,output,cache_read,cache_write,reasoning,total,cost,messages)
 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def api(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Authorization": _AUTH, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main():
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - WINDOW_DAYS * 86400 * 1000
    members = {m["email"]: m for m in api("/teams/members").get("teamMembers", []) if m.get("email")}

    # 分页拉事件,按 (email, day, model) 聚合
    agg = {}   # key -> [input,output,cacheRead,cacheWrite,total,costCents,count]
    page = 1
    total_events = 0
    while page <= MAX_PAGES:
        resp = api("/teams/filtered-usage-events", "POST",
                   {"startDate": start_ms, "endDate": now_ms, "page": page, "pageSize": PAGE_SIZE})
        evs = resp.get("usageEvents", [])
        for e in evs:
            tu = e.get("tokenUsage") or {}
            if not tu:
                continue
            email = e.get("userEmail")
            if not email:
                continue
            ts = int(e.get("timestamp", "0"))
            day = datetime.datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")
            model = e.get("model") or "unknown"
            k = (email, day, model)
            a = agg.setdefault(k, [0, 0, 0, 0, 0, 0.0, 0])
            a[0] += int(tu.get("inputTokens") or 0)
            a[1] += int(tu.get("outputTokens") or 0)
            a[2] += int(tu.get("cacheReadTokens") or 0)
            a[3] += int(tu.get("cacheWriteTokens") or 0)
            a[5] += float(e.get("chargedCents") or tu.get("totalCents") or 0)
            a[6] += 1
        total_events += len(evs)
        if not resp.get("pagination", {}).get("hasNextPage"):
            break
        page += 1

    # 飞连身份(按 email 缓存)
    fc = None
    root = None
    try:
        sys.path.insert(0, HERE)
        from feilian_client import FeilianClient
        fc = FeilianClient()
        root = fc.root_department_id()
    except Exception as ex:
        print("飞连不可用,部门/头像留空:", ex)
    # 预加载 people 表已知身份 → 只对新人调飞连(把 12 分钟压到几分钟)
    icache = {}
    try:
        _pc = sqlite3.connect(DB)
        for em, nm, av, dp in _pc.execute("SELECT email,name,avatar,dept FROM people").fetchall():
            if nm and dp and dp != "unknown":
                icache[em] = {"name": nm, "avatar": av or "", "dept": dp}
        _pc.close()
        print("预加载已知身份 %d 人(跳过飞连)" % len(icache))
    except Exception:
        pass

    def resolve(email):
        if email in icache:
            return icache[email]
        fb = (members.get(email, {}) or {}).get("name") or email.split("@")[0]
        out = {"name": fb, "dept": "unknown", "avatar": ""}
        if fc and root:
            try:
                data = fc._request("GET", "/api/open/v2/user/list",
                                   query={"department_id": root, "fetch_child": "true",
                                          "query": email, "limit": 5})
                for u in (data or {}).get("user_list") or []:
                    if (u.get("email") or "").lower() == email.lower():
                        out = {"name": u.get("full_name") or fb,
                               "dept": u.get("department_path") or "unknown",
                               "avatar": u.get("avatar") or ""}
                        break
            except Exception:
                pass
        icache[email] = out
        return out

    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS people(email TEXT PRIMARY KEY, name TEXT, avatar TEXT, dept TEXT)")
    # 先清掉本源旧数据(窗口外的日桶 + 旧 lifetime),避免陈旧
    c.execute("DELETE FROM usage WHERE source='cursor'")

    # 日桶
    life = {}  # (email, model) -> [in,out,cr,cw,total,cost,msgs]
    people_seen = {}
    for (email, day, model), a in agg.items():
        ident = resolve(email)
        people_seen[email] = ident
        total = a[0] + a[1] + a[2] + a[3]
        cost = a[5] / 100.0
        c.execute(_UPSERT, (email, ident["dept"], "day", day, "cursor", "Cursor", "", model,
                            a[0], a[1], a[2], a[3], 0, total, round(cost, 4), a[6]))
        lk = (email, model)
        L = life.setdefault(lk, [0, 0, 0, 0, 0, 0.0, 0])
        L[0] += a[0]; L[1] += a[1]; L[2] += a[2]; L[3] += a[3]
        L[5] += cost; L[6] += a[6]

    # lifetime(窗口累计)
    for (email, model), L in life.items():
        ident = people_seen.get(email) or resolve(email)
        total = L[0] + L[1] + L[2] + L[3]
        c.execute(_UPSERT, (email, ident["dept"], "lifetime", "all", "cursor", "Cursor", "", model,
                            L[0], L[1], L[2], L[3], 0, total, round(L[5], 4), L[6]))

    # people
    for email, ident in people_seen.items():
        c.execute("INSERT OR REPLACE INTO people(email,name,avatar,dept) VALUES(?,?,?,?)",
                  (email, ident["name"], ident["avatar"], ident["dept"]))
    c.commit()
    c.close()
    print("Cursor sync: 事件 %d, 入库 %d 人(团队 %d),窗口 %d 天" %
          (total_events, len(people_seen), len(members), WINDOW_DAYS))


if __name__ == "__main__":
    main()
