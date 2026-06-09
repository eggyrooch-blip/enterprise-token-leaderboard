"""演示/冒烟数据：通过收集端 API 灌入样例 token + 代码指标，用于本地验收看板。
既是 demo 数据，也是端到端冒烟测试（验证 ingest -> 存储 -> 展示 全链路）。

用法：
  COLLECTOR_URL=http://localhost:8088 COLLECTOR_TOKEN=devtoken python seed_demo.py
"""
from __future__ import annotations

import json
import os
import random
import urllib.request
from datetime import date, timedelta

URL = os.environ.get("COLLECTOR_URL", "http://localhost:8088").rstrip("/")
TOKEN = os.environ.get("COLLECTOR_TOKEN", "devtoken")

PEOPLE = [
    ("zhangsan@example.com", "infra"),
    ("lisi@example.com", "infra"),
    ("wangwu@example.com", "platform"),
    ("zhaoliu@example.com", "platform"),
    ("qian@example.com", "mobile"),
]
TOOLS = [("claude_code", "claude-opus-4-8"), ("codex", "gpt-5.5"), ("cursor", "claude-sonnet-4-6")]


def _post(path: str, payload: dict) -> None:
    req = urllib.request.Request(
        URL + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        print(path, json.loads(r.read()))


def main() -> None:
    random.seed(7)
    for email, dept in PEOPLE:
        urecs, crecs = [], []
        for i in range(14):
            day = (date.today() - timedelta(days=i)).isoformat()
            for tool, model in TOOLS:
                src = "api" if tool == "codex" else "subscription"
                inp = random.randint(20_000, 200_000)
                out = random.randint(5_000, 60_000)
                urecs.append({"usage_date": day, "source": src, "tool": tool, "model": model,
                              "input_tokens": inp, "output_tokens": out,
                              "cache_read_tokens": inp // 2,
                              "cost_usd": round((inp + out) / 1_000_000 * 5, 4)})
            sug = random.randint(200, 1500)
            crecs.append({"usage_date": day, "source": "cursor", "tool": "cursor",
                          "lines_suggested": sug, "lines_accepted": int(sug * random.uniform(.3, .8)),
                          "lines_added": sug, "suggestions_shown": sug // 5,
                          "suggestions_accepted": sug // 10})
        _post("/v1/usage/report", {"email": email, "dept": dept, "records": urecs})
        _post("/v1/code/report", {"email": email, "dept": dept, "records": crecs})
    print("seeded demo data for", len(PEOPLE), "people")


if __name__ == "__main__":
    main()
