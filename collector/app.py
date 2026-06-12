"""中心收集端：接收客户端(订阅制)上报 + 提供排行榜查询。

设计要点：
- POST /v1/usage/report 做幂等 upsert，主键 (email, usage_date, source, tool, model)。
- 鉴权用 Bearer token（COLLECTOR_API_TOKENS，逗号分隔，可给不同部门发不同 token）。
- LiteLLM 那一路由 litellm_sync.py 单独灌入同一张表(source='api')，所以这里不耦合 LiteLLM。
- 只接收 token 计数/成本，绝不接收 prompt 或代码内容。
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import List, Mapping, Optional

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

DATABASE_URL = os.environ["DATABASE_URL"]
API_TOKENS = {t.strip() for t in os.environ.get("COLLECTOR_API_TOKENS", "").split(",") if t.strip()}

app = FastAPI(title="Token Leaderboard Collector", version="1.0.0")
_pool: Optional[asyncpg.Pool] = None


@app.on_event("startup")
async def _startup() -> None:
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as fh:
        async with _pool.acquire() as conn:
            await conn.execute(fh.read())


def require_token(authorization: str = Header(default="")) -> None:
    if not API_TOKENS:  # 未配置 token 时拒绝启动式保护
        raise HTTPException(500, "collector has no API tokens configured")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    if authorization.split(" ", 1)[1] not in API_TOKENS:
        raise HTTPException(403, "invalid token")


def _coerce_date(v: date | str) -> date:
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v), "%Y-%m-%d").date()


def months_overlapped(start: date | str, end: date | str) -> int:
    """闭区间 [start, end] 触达的自然月数。"""
    start_d = _coerce_date(start)
    end_d = _coerce_date(end)
    if end_d < start_d:
        return 1
    return (end_d.year - start_d.year) * 12 + (end_d.month - start_d.month) + 1


def _subscription_months(days: int) -> int:
    today = date.today()
    span = max(int(days or 1), 1)
    start = today - timedelta(days=span - 1)
    return months_overlapped(start, today)


async def _fetch_subscriptions(conn: asyncpg.Connection) -> tuple[dict[str, list[dict]], dict[str, float], dict[str, dict[str, str]]]:
    rows = await conn.fetch(
        """
        SELECT email, tool, tier, monthly_fee_usd, seats, display_name, dept
        FROM subscriptions
        ORDER BY email, tool, tier
        """
    )
    subs_by_email: dict[str, list[dict]] = {}
    fee_by_email: dict[str, float] = {}
    profile_by_email: dict[str, dict[str, str]] = {}
    for r in rows:
        email = (r["email"] or "").strip()
        if not email:
            continue
        subs_by_email.setdefault(email, []).append({
            "tool": r["tool"],
            "tier": r["tier"],
            "fee": float(r["monthly_fee_usd"] or 0),
            "seats": int(r["seats"] or 1),
        })
        fee_by_email[email] = fee_by_email.get(email, 0.0) + float(r["monthly_fee_usd"] or 0)
        profile = profile_by_email.setdefault(email, {})
        if r["display_name"] and not profile.get("display_name"):
            profile["display_name"] = r["display_name"]
        if r["dept"] and not profile.get("dept"):
            profile["dept"] = r["dept"]
    return subs_by_email, fee_by_email, profile_by_email


def _subscription_text(subs: list[dict]) -> str:
    return "；".join(
        f'{s.get("tool")}/{s.get("tier")} ${float(s.get("fee") or 0):g}/月'
        f' ×{int(s.get("seats") or 1)}' if int(s.get("seats") or 1) > 1
        else f'{s.get("tool")}/{s.get("tier")} ${float(s.get("fee") or 0):g}/月'
        for s in subs
    )


def _aggregate_rows_to_email(
    rows: list[Mapping[str, object]],
    fee_by_email: Mapping[str, float],
    months: int,
    profile_by_email: Mapping[str, Mapping[str, str]],
) -> list[dict[str, object]]:
    """Collapse usage rows to one row per email, then add subscription fee once."""
    by_email: dict[str, dict[str, object]] = {}
    best_dept_tokens: dict[str, int] = {}
    for row in rows:
        email = str(row.get("email") or "").strip()
        if not email:
            continue
        total_tokens = int(row.get("total_tokens") or row.get("t") or 0)
        gateway_cost = float(row.get("gateway_cost") or row.get("c") or 0)
        api_tokens = int(row.get("api") or 0)
        subscription_tokens = int(row.get("sub") or 0)
        dept = str(row.get("dept") or "").strip()
        profile_dept = str(profile_by_email.get(email, {}).get("dept") or "").strip()
        person = by_email.get(email)
        if person is None:
            by_email[email] = {
                "email": email,
                "dept": profile_dept or dept or "unknown",
                "total_tokens": total_tokens,
                "gateway_cost": gateway_cost,
                "api_tokens": api_tokens,
                "subscription_tokens": subscription_tokens,
                "cost_usd": 0.0,
            }
            best_dept_tokens[email] = total_tokens if dept else -1
            continue
        person["total_tokens"] = int(person["total_tokens"] or 0) + total_tokens
        person["gateway_cost"] = float(person["gateway_cost"] or 0.0) + gateway_cost
        person["api_tokens"] = int(person["api_tokens"] or 0) + api_tokens
        person["subscription_tokens"] = int(person["subscription_tokens"] or 0) + subscription_tokens
        if not profile_dept and dept and total_tokens > best_dept_tokens[email]:
            person["dept"] = dept
            best_dept_tokens[email] = total_tokens
    for email, person in by_email.items():
        person["gateway_cost"] = round(float(person["gateway_cost"] or 0.0), 4)
        person["cost_usd"] = round(float(person["gateway_cost"] or 0.0) + fee_by_email.get(email, 0.0) * months, 4)
    return list(by_email.values())


class UsageRecord(BaseModel):
    usage_date: date
    tool: str
    model: str = "unknown"
    # source 随 record 走（不写死），任意来源标签都可入库：
    # 'subscription' | 'api' | 'cursor_admin' | 'bedrock' | ...，新增采集源无需改表/改接口。
    source: str = Field(default="subscription", pattern="^[a-z0-9_]{1,32}$")
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


class ReportPayload(BaseModel):
    email: str
    dept: str = "unknown"
    # 兼容老客户端：可在 payload 顶层给默认 source，record 未带 source 时回退到它。
    source: str = Field(default="subscription", pattern="^[a-z0-9_]{1,32}$")
    records: List[UsageRecord]


UPSERT = """
INSERT INTO usage_daily (email, dept, usage_date, source, tool, model,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
    total_tokens, cost_usd, updated_at)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12, now())
ON CONFLICT (email, usage_date, source, tool, model) DO UPDATE SET
    dept=EXCLUDED.dept,
    input_tokens=EXCLUDED.input_tokens,
    output_tokens=EXCLUDED.output_tokens,
    cache_read_tokens=EXCLUDED.cache_read_tokens,
    cache_write_tokens=EXCLUDED.cache_write_tokens,
    total_tokens=EXCLUDED.total_tokens,
    cost_usd=EXCLUDED.cost_usd,
    updated_at=now();
"""


@app.post("/v1/usage/report", dependencies=[Depends(require_token)])
async def report(payload: ReportPayload) -> dict:
    assert _pool is not None
    async with _pool.acquire() as conn:
        async with conn.transaction():
            for r in payload.records:
                total = r.total_tokens or (
                    r.input_tokens + r.output_tokens + r.cache_read_tokens + r.cache_write_tokens
                )
                source = r.source or payload.source
                await conn.execute(
                    UPSERT, payload.email, payload.dept, r.usage_date, source,
                    r.tool, r.model, r.input_tokens, r.output_tokens, r.cache_read_tokens,
                    r.cache_write_tokens, total, r.cost_usd,
                )
    return {"ok": True, "upserted": len(payload.records)}


class CodeRecord(BaseModel):
    usage_date: date
    tool: str
    source: str = Field(default="cursor", pattern="^[a-z0-9_]{1,32}$")
    lines_suggested: int = 0
    lines_accepted: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    suggestions_shown: int = 0
    suggestions_accepted: int = 0
    commits: int = 0
    lines_surviving: int = 0


class CodeReportPayload(BaseModel):
    email: str
    dept: str = "unknown"
    records: List[CodeRecord]


CODE_UPSERT = """
INSERT INTO code_daily (email, dept, usage_date, source, tool,
    lines_suggested, lines_accepted, lines_added, lines_removed,
    suggestions_shown, suggestions_accepted, commits, lines_surviving, updated_at)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13, now())
ON CONFLICT (email, usage_date, source, tool) DO UPDATE SET
    dept=EXCLUDED.dept,
    lines_suggested=EXCLUDED.lines_suggested,
    lines_accepted=EXCLUDED.lines_accepted,
    lines_added=EXCLUDED.lines_added,
    lines_removed=EXCLUDED.lines_removed,
    suggestions_shown=EXCLUDED.suggestions_shown,
    suggestions_accepted=EXCLUDED.suggestions_accepted,
    commits=EXCLUDED.commits,
    lines_surviving=EXCLUDED.lines_surviving,
    updated_at=now();
"""


@app.post("/v1/code/report", dependencies=[Depends(require_token)])
async def code_report(payload: CodeReportPayload) -> dict:
    assert _pool is not None
    async with _pool.acquire() as conn:
        async with conn.transaction():
            for r in payload.records:
                await conn.execute(
                    CODE_UPSERT, payload.email, payload.dept, r.usage_date, r.source, r.tool,
                    r.lines_suggested, r.lines_accepted, r.lines_added, r.lines_removed,
                    r.suggestions_shown, r.suggestions_accepted, r.commits, r.lines_surviving,
                )
    return {"ok": True, "upserted": len(payload.records)}


@app.get("/v1/leaderboard", dependencies=[Depends(require_token)])
async def leaderboard(days: int = 30, source: str = "all", limit: int = 100) -> dict:
    assert _pool is not None
    where_source = "" if source == "all" else "AND source = $2"
    args: list = [days]
    if source != "all":
        args.append(source)
    sql = f"""
        SELECT email, dept,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COALESCE(SUM(cost_usd) FILTER (WHERE source IN ('api','litellm')), 0) AS gateway_cost
        FROM usage_daily
        WHERE usage_date >= current_date - ($1::int - 1)
        {where_source}
        GROUP BY email, dept
        ORDER BY total_tokens DESC, email ASC;
    """
    async with _pool.acquire() as conn:
        subs_by_email, fee_by_email, profile_by_email = await _fetch_subscriptions(conn)
        rows = await conn.fetch(sql, *args)
    months = _subscription_months(days)
    aggregated_rows = _aggregate_rows_to_email([dict(r) for r in rows], fee_by_email, months, profile_by_email)
    ranking = []
    seen_emails = set()
    # 公司实付口径：订阅费按窗口触达自然月数整月计；usage cost 只取 api/litellm 实销，
    # 排除 subscription 牌价；纯订阅人也进榜。
    for r in aggregated_rows:
        email = r["email"]
        seen_emails.add(email)
        ranking.append({
            "email": email,
            "dept": r["dept"] or "unknown",
            "total_tokens": r["total_tokens"] or 0,
            "cost_usd": r["cost_usd"] or 0,
            "subs": list(subs_by_email.get(email, [])),
        })
    ranking.sort(key=lambda r: (-int(r["total_tokens"] or 0), -float(r["cost_usd"] or 0), r["email"]))
    return {"days": days, "source": source,
            "ranking": ranking[:limit]}


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


# ---- 自带展示页（MVP：无需 Grafana 即可看榜）----
# 只读、内网可见即可；生产可放在反代后或加 VIEW_TOKEN。数据直接查库，不经鉴权 API。

def _table(title: str, headers: list[str], rows: list[tuple]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) \
        or f'<tr><td colspan="{len(headers)}" class="empty">暂无数据</td></tr>'
    return f"<section><h2>{title}</h2><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></section>"


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(days: int = 30) -> str:
    assert _pool is not None
    window = "current_date - ($1::int - 1)"
    async with _pool.acquire() as conn:
        subs_by_email, fee_by_email, profile_by_email = await _fetch_subscriptions(conn)
        ppl = await conn.fetch(f"""
            SELECT email, dept, COALESCE(SUM(total_tokens), 0) t,
                   COALESCE(SUM(cost_usd) FILTER (WHERE source IN ('api','litellm')), 0) c,
                   COALESCE(SUM(total_tokens) FILTER (WHERE source='api'), 0) api,
                   COALESCE(SUM(total_tokens) FILTER (WHERE source='subscription'), 0) sub
            FROM usage_daily WHERE usage_date >= {window}
            GROUP BY email, dept""", days)
        depts = await conn.fetch(f"""
            SELECT dept, SUM(total_tokens) t, ROUND(SUM(cost_usd),2) c
            FROM usage_daily WHERE usage_date >= {window}
            GROUP BY dept ORDER BY t DESC""", days)
        tools = await conn.fetch(f"""
            SELECT tool, SUM(total_tokens) t FROM usage_daily WHERE usage_date >= {window}
            GROUP BY tool ORDER BY t DESC""", days)
        code = await conn.fetch(f"""
            SELECT dept, SUM(lines_accepted) acc, SUM(lines_suggested) sug,
                   ROUND(100.0*SUM(lines_accepted)/NULLIF(SUM(lines_suggested),0),1) rate
            FROM code_daily WHERE usage_date >= {window}
            GROUP BY dept ORDER BY acc DESC""", days)
    months = _subscription_months(days)
    aggregated_rows = _aggregate_rows_to_email([dict(r) for r in ppl], fee_by_email, months, profile_by_email)
    ppl_rows = []
    seen_emails = set()
    for r in aggregated_rows:
        email = r["email"]
        seen_emails.add(email)
        subs = list(subs_by_email.get(email, []))
        ppl_rows.append({
            "email": email,
            "dept": r["dept"] or "unknown",
            "t": r["total_tokens"] or 0,
            "c": r["cost_usd"] or 0,
            "api": r["api_tokens"] or 0,
            "sub": r["subscription_tokens"] or 0,
            "subs": subs,
        })
    ppl_rows.sort(key=lambda r: (-int(r["t"] or 0), -float(r["c"] or 0), r["email"]))
    ppl_rows = ppl_rows[:50]

    def fmt(n):
        return f"{int(n or 0):,}"

    sections = [
        _table("个人 Token 榜 (Top 50)", ["#", "邮箱", "部门", "Token", "其中 API", "其中订阅", "订阅套餐", "公司实付$"],
               [(i + 1, r["email"], r["dept"], fmt(r["t"]), fmt(r["api"]), fmt(r["sub"]),
                 _subscription_text(r["subs"]) or "-", r["c"] or 0)
                for i, r in enumerate(ppl_rows)]),
        _table("部门 Token 榜", ["部门", "Token", "成本$"],
               [(r["dept"], fmt(r["t"]), r["c"] or 0) for r in depts]),
        _table("工具维度", ["工具", "Token"], [(r["tool"], fmt(r["t"])) for r in tools]),
        _table("代码采纳率 / 有效代码行 (部门)", ["部门", "有效行(采纳)", "建议行", "采纳率%"],
               [(r["dept"], fmt(r["acc"]), fmt(r["sug"]), r["rate"] if r["rate"] is not None else "-")
                for r in code]),
    ]
    css = """body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#1d1d1f;background:#f5f5f7}
    h1{font-size:22px}h2{font-size:16px;margin-top:28px}section{max-width:960px}
    table{border-collapse:collapse;width:100%;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08)}
    th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #eee;font-size:13px}
    th{background:#fafafa;font-weight:600}tr:hover td{background:#f9f9fb}
    td:nth-child(n+4){text-align:right;font-variant-numeric:tabular-nums}.empty{text-align:center;color:#888}
    .bar{margin:8px 0 4px}a{color:#06c;text-decoration:none}"""
    nav = " ".join(f'<a href="?days={d}">{d}天</a>' for d in (7, 30, 90))
    return (f"<!doctype html><html lang=zh><head><meta charset=utf-8>"
            f"<title>Token 消耗排行榜</title><style>{css}</style></head><body>"
            f"<h1>🏅 企业 AI Agent 用量看板</h1>"
            f'<div class=bar>统计窗口：最近 {days} 天 &nbsp;|&nbsp; 切换：{nav}</div>'
            + "".join(sections) +
            "</body></html>")
