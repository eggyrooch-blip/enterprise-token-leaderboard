#!/usr/bin/env python3
"""客户端上报 runner：解析身份 -> 跑已启用的采集源 -> 上报到收集端。

只依赖 python3 标准库。由 launchd 每天静默触发（弱感知/无感知）。
幂等：每次重传最近 LOOKBACK_DAYS 天，收集端按 (email,date,source,tool,model) 覆盖。

只上报 token 计数/成本，绝不读取或上传 prompt / 代码内容。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collectors  # noqa: E402
import identity  # noqa: E402

CONF_PATH = os.environ.get("TOKREPORT_CONF", "/etc/tokreport.conf")


def load_conf(path: str) -> dict:
    conf: dict[str, str] = {}
    if not os.path.exists(path):
        return conf
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip().strip('"')
    return conf


def post(conf: dict, email: str, dept: str, records: list[dict]) -> None:
    url = (os.environ.get("TOKREPORT_COLLECTOR_URL") or conf["COLLECTOR_URL"]).rstrip("/")
    token = os.environ.get("TOKREPORT_COLLECTOR_TOKEN") or conf["COLLECTOR_TOKEN"]
    body = json.dumps({"email": email, "dept": dept, "records": records}).encode()
    req = urllib.request.Request(
        url + "/v1/usage/report", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def main() -> int:
    conf = load_conf(CONF_PATH)
    email, dept = identity.resolve(conf)
    lookback = int(conf.get("LOOKBACK_DAYS", "3"))
    names = [n for n in conf.get("COLLECTORS", "tokscale").split(",") if n.strip()]

    active = collectors.build(names, conf)
    if not active:
        print("no available collectors on this machine")
        return 0

    all_records: list[dict] = []
    for col in active:
        for i in range(lookback):
            day = date.today() - timedelta(days=i)
            try:
                all_records.extend(col.collect(day))
            except (subprocess.CalledProcessError, OSError, ValueError) as e:
                print(f"{col.name} failed for {day}: {e}", file=sys.stderr)

    if not all_records:
        print("no usage to report")
        return 0
    post(conf, email, dept, all_records)
    print(f"reported {len(all_records)} records as {email} "
          f"via [{', '.join(c.name for c in active)}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
