#!/usr/bin/env python3
"""部门维度报告：拉某部门真实花名册 + 活跃终端 + 覆盖率，合入已接入机器的 token。

诚实口径：token 只来自已铺采集端的机器（当前=本机/Alex Chen）；其余成员标"待接入"。
飞连给的是真实组织/终端数据，token 缺口如实呈现，不编造。

用法：  python3 pipeline/dept_report.py 基础技术部
产出：  dashboard/data/dept.{json,js}
"""
from __future__ import annotations
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "collector"))
exec(open(ROOT / "pipeline" / "build_report.py").read().split("def collect_usage")[0])  # load_env etc.


def walk(nodes, path=""):
    for n in nodes or []:
        full = (path + "/" + n["name"]).strip("/")
        yield n["id"], n["name"], full, n
        yield from walk(n.get("sub_departments"), full)


def main():
    dept_name = sys.argv[1] if len(sys.argv) > 1 else "基础技术部"
    load_env(ROOT / "pipeline" / ".env")
    from feilian_client import FeilianClient
    fc = FeilianClient()

    # 1) 定位部门
    did = full = None
    for i, name, fl, node in walk(fc.department_tree()):
        if name == dept_name:
            did, full = i, fl
            break
    if not did:
        print("未找到部门:", dept_name, file=sys.stderr); sys.exit(1)

    # 2) 花名册（含子部门，在职）
    roster = []
    off = 0
    while True:
        data = fc._request("GET", "/api/open/v2/user/list",
                           query={"department_id": did, "fetch_child": "true",
                                  "status": 0, "limit": 200, "offset": off})
        ul = (data or {}).get("user_list") or []
        roster += ul
        off += len(ul)
        if len(ul) < 200 or off >= (data.get("count") or 0):
            break

    # 3) 本机 token（已接入数据），按邮箱/姓名归属
    usage = {}
    up = ROOT / "dashboard" / "data" / "usage.json"
    if up.exists():
        rpt = json.loads(up.read_text())
        person = rpt.get("person", {})
        usage[(person.get("email") or "").lower()] = rpt.get("usage", {}).get("totals", {})
        usage_name = person.get("name")
    else:
        usage_name = None

    # 4) 逐人拉活跃终端 + 合 token
    members = []
    active_mac_total = 0
    for u in roster:
        email = (u.get("email") or "").lower()
        uid = u.get("id")
        devs = []
        try:
            d = fc._request("GET", "/api/open/v1/device/search",
                           query={"id": uid, "status": 1, "limit": 50})
            devs = (d or {}).get("devices") or []
        except Exception:
            pass
        macs = [x for x in devs if (x.get("os") or "").lower() == "mac"]
        active_mac_total += len(macs)
        tot = usage.get(email)
        ingested = tot is not None or (usage_name and u.get("full_name") == usage_name)
        if ingested and tot is None:
            tot = usage.get(list(usage.keys())[0]) if usage else None
        members.append({
            "name": u.get("full_name"),
            "email": u.get("email"),
            "dept": (u.get("department_path") or "").split("/")[-1],
            "active_macs": len(macs),
            "ingested": bool(ingested),
            "tokens": (tot or {}).get("tokens", 0) if ingested else 0,
            "cost": (tot or {}).get("cost", 0.0) if ingested else 0.0,
            "messages": (tot or {}).get("messages", 0) if ingested else 0,
        })

    members.sort(key=lambda m: (m["ingested"], m["tokens"]), reverse=True)
    ingested_n = sum(1 for m in members if m["ingested"])

    # 子部门聚合
    sub = {}
    for m in members:
        s = sub.setdefault(m["dept"], {"dept": m["dept"], "headcount": 0, "active_macs": 0,
                                       "tokens": 0, "ingested": 0})
        s["headcount"] += 1; s["active_macs"] += m["active_macs"]
        s["tokens"] += m["tokens"]; s["ingested"] += 1 if m["ingested"] else 0
    subs = sorted(sub.values(), key=lambda x: x["tokens"], reverse=True)

    report = {
        "department": full, "department_name": dept_name,
        "headcount": len(roster), "active_mac": active_mac_total,
        "ingested": ingested_n,
        "coverage_pct": round(ingested_n / active_mac_total * 100, 1) if active_mac_total else 0,
        "members": members, "subs": subs,
    }
    out = ROOT / "dashboard" / "data" / "dept.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (out.parent / "dept.js").write_text("window.__DEPT__ = " + json.dumps(report, ensure_ascii=False) + ";\n")

    print(f"部门: {full}", file=sys.stderr)
    print(f"在职 {len(roster)} 人 · 活跃 Mac {active_mac_total} 台 · 已接入 {ingested_n} 台 "
          f"· 覆盖率 {report['coverage_pct']}%", file=sys.stderr)
    print("子部门:", [(s["dept"], s["headcount"], "Mac", s["active_macs"]) for s in subs], file=sys.stderr)
    print("榜首:", [(m["name"], m["dept"], m["tokens"], "接入" if m["ingested"] else "待接入")
                   for m in members[:6]], file=sys.stderr)


if __name__ == "__main__":
    main()
