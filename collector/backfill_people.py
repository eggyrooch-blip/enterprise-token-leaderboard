#!/usr/bin/env python3
# 回填 people 表:对 usage 里已有的每个 email,经飞连按邮箱反查 中文姓名 + 头像 + 完整部门路径。
# 用于 people 表建立之前已上报的用户。可重复跑(INSERT OR REPLACE)。
# 用法(在收集端目录,有 .env)：python3 backfill_people.py [tok.db]
import os, sqlite3, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, ".env"), os.path.join(HERE, "..", "pipeline", ".env")):
    if os.path.exists(cand):
        for l in open(cand):
            l = l.strip()
            if l and not l.startswith("#") and "=" in l:
                k, v = l.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, HERE)
from feilian_client import FeilianClient

DB = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DEV_DB", "tok.db")
fc = FeilianClient()
root = fc.root_department_id()
c = sqlite3.connect(DB)
c.execute("CREATE TABLE IF NOT EXISTS people(email TEXT PRIMARY KEY, name TEXT, avatar TEXT, dept TEXT)")
emails = [r[0] for r in c.execute("SELECT DISTINCT email FROM usage").fetchall()]
done = 0
for e in emails:
    if not e or e.startswith("sn:"):
        continue
    try:
        data = fc._request("GET", "/api/open/v2/user/list",
                           query={"department_id": root, "fetch_child": "true",
                                  "query": e, "limit": 5})
        u = None
        for x in (data or {}).get("user_list") or []:
            if (x.get("email") or "").lower() == e.lower():
                u = x
                break
        if u:
            c.execute("INSERT OR REPLACE INTO people(email, name, avatar, dept) VALUES(?,?,?,?)",
                      (e, u.get("full_name"), u.get("avatar") or "", u.get("department_path") or ""))
            done += 1
            print("backfill:", e, "->", u.get("full_name"), "avatar=" + ("yes" if u.get("avatar") else "no"))
        else:
            print("no match:", e)
    except Exception as ex:
        print("err:", e, ex)
c.commit()
print("done, %d people backfilled" % done)
