"""把 LiteLLM 的用量灌进统一表 (source='api')。每天定时跑一次即可。

实现：拉 LiteLLM 的 /spend/logs，按 (user, 日期, model) 聚合后 upsert 进 usage_daily。
不同 LiteLLM 版本字段略有差异，解析集中在 _parse_log() 一处，按你的版本调整即可。

幂等：每次重跑最近 LOOKBACK 天并覆盖，离线补数安全。
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

import asyncpg
import httpx

DATABASE_URL = os.environ["DATABASE_URL"]
LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"].rstrip("/")
LITELLM_MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]
LOOKBACK_DAYS = int(os.environ.get("LITELLM_LOOKBACK_DAYS", "3"))

_user_map: dict[str, str] = {}
if os.environ.get("LITELLM_USER_MAP"):
    with open(os.environ["LITELLM_USER_MAP"]) as fh:
        _user_map = json.load(fh)

UPSERT = """
INSERT INTO usage_daily (email, dept, usage_date, source, tool, model,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
    total_tokens, cost_usd, updated_at)
VALUES ($1,'unknown',$2,'api','litellm',$3,$4,$5,0,0,$6,$7, now())
ON CONFLICT (email, usage_date, source, tool, model) DO UPDATE SET
    input_tokens=EXCLUDED.input_tokens,
    output_tokens=EXCLUDED.output_tokens,
    total_tokens=EXCLUDED.total_tokens,
    cost_usd=EXCLUDED.cost_usd,
    updated_at=now();
"""


def _parse_log(row: dict) -> tuple[str, date, str, int, int, int, float] | None:
    """LiteLLM spend log -> (email, day, model, in, out, total, cost). 改这里适配你的版本。"""
    user = row.get("user") or row.get("user_id") or row.get("end_user")
    if not user:
        return None
    email = _user_map.get(user, user)  # user 本身就是 email 时直接用
    ts = row.get("startTime") or row.get("created_at") or row.get("start_time")
    if not ts:
        return None
    day = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
    model = row.get("model") or "unknown"
    pin = int(row.get("prompt_tokens") or 0)
    pout = int(row.get("completion_tokens") or 0)
    total = int(row.get("total_tokens") or (pin + pout))
    cost = float(row.get("spend") or 0.0)
    return email, day, model, pin, pout, total, cost


async def main() -> None:
    end = date.today()
    start = end - timedelta(days=LOOKBACK_DAYS - 1)
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{LITELLM_BASE_URL}/spend/logs",
            params={"start_date": start.isoformat(), "end_date": end.isoformat()},
            headers=headers,
        )
        resp.raise_for_status()
        logs = resp.json()

    # 聚合到 (email, day, model)
    agg: dict[tuple, list] = defaultdict(lambda: [0, 0, 0, 0.0])
    for row in logs:
        parsed = _parse_log(row)
        if not parsed:
            continue
        email, day, model, pin, pout, total, cost = parsed
        a = agg[(email, day, model)]
        a[0] += pin
        a[1] += pout
        a[2] += total
        a[3] += cost

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        async with conn.transaction():
            for (email, day, model), (pin, pout, total, cost) in agg.items():
                await conn.execute(UPSERT, email, day, model, pin, pout, total, cost)
    await pool.close()
    print(f"litellm_sync: upserted {len(agg)} rows for {start}..{end}")


if __name__ == "__main__":
    asyncio.run(main())
