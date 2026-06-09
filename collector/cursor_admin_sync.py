"""把 Cursor 的代码采纳指标灌进 code_daily (source='cursor')。每天定时跑一次。

数据源：Cursor Admin API  POST /teams/daily-usage-data  (需团队 admin API key)。
返回每人每天的 行数/采纳/Tab 指标，按官方口径折算成「建议行/采纳行」。
字段在不同时期可能调整，解析集中在 _parse() 一处。

幂等：每次回看最近 LOOKBACK 天并覆盖。只取聚合指标，不取任何代码内容。
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone

import asyncpg
import httpx

DATABASE_URL = os.environ["DATABASE_URL"]
CURSOR_API_BASE = os.environ.get("CURSOR_API_BASE", "https://api.cursor.com").rstrip("/")
CURSOR_API_KEY = os.environ["CURSOR_API_KEY"]
LOOKBACK_DAYS = int(os.environ.get("CURSOR_LOOKBACK_DAYS", "3"))

UPSERT = """
INSERT INTO code_daily (email, dept, usage_date, source, tool,
    lines_suggested, lines_accepted, lines_added, lines_removed,
    suggestions_shown, suggestions_accepted, commits, lines_surviving, updated_at)
VALUES ($1,'unknown',$2,'cursor','cursor',$3,$4,$5,$6,$7,$8,0,0, now())
ON CONFLICT (email, usage_date, source, tool) DO UPDATE SET
    lines_suggested=EXCLUDED.lines_suggested,
    lines_accepted=EXCLUDED.lines_accepted,
    lines_added=EXCLUDED.lines_added,
    lines_removed=EXCLUDED.lines_removed,
    suggestions_shown=EXCLUDED.suggestions_shown,
    suggestions_accepted=EXCLUDED.suggestions_accepted,
    updated_at=now();
"""


def _i(d: dict, k: str) -> int:
    try:
        return int(d.get(k) or 0)
    except (TypeError, ValueError):
        return 0


def _parse(row: dict):
    """Cursor daily-usage row -> upsert 参数元组。按官方口径改这里即可。"""
    email = row.get("email") or row.get("userEmail")
    if not email:
        return None
    ms = row.get("date") or row.get("day")
    if ms is None:
        return None
    day = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date()

    added = _i(row, "totalLinesAdded")
    removed = _i(row, "totalLinesDeleted")
    tabs_shown = _i(row, "totalTabsShown")
    tabs_accepted = _i(row, "totalTabsAccepted")
    acc_added = _i(row, "acceptedLinesAdded")
    acc_removed = _i(row, "acceptedLinesDeleted")

    lines_suggested = added + removed + tabs_shown
    lines_accepted = acc_added + acc_removed + tabs_accepted
    return (email, day, lines_suggested, lines_accepted, added, removed,
            tabs_shown, tabs_accepted)


async def main() -> None:
    end = date.today()
    start = end - timedelta(days=LOOKBACK_DAYS - 1)
    start_ms = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{CURSOR_API_BASE}/teams/daily-usage-data",
            json={"startDate": start_ms, "endDate": end_ms},
            auth=(CURSOR_API_KEY, ""),  # Cursor Admin API: key 作为 Basic auth 用户名
        )
        resp.raise_for_status()
        rows = resp.json().get("data", resp.json() if isinstance(resp.json(), list) else [])

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    n = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for row in rows:
                parsed = _parse(row)
                if parsed:
                    await conn.execute(UPSERT, *parsed)
                    n += 1
    await pool.close()
    print(f"cursor_admin_sync: upserted {n} rows for {start}..{end}")


if __name__ == "__main__":
    asyncio.run(main())
