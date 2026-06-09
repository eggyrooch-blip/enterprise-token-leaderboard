"""中心收集端：接收客户端(订阅制)上报 + 提供排行榜查询。

设计要点：
- POST /v1/usage/report 做幂等 upsert，主键 (email, usage_date, source, tool, model)。
- 鉴权用 Bearer token（COLLECTOR_API_TOKENS，逗号分隔，可给不同部门发不同 token）。
- LiteLLM 那一路由 litellm_sync.py 单独灌入同一张表(source='api')，所以这里不耦合 LiteLLM。
- 只接收 token 计数/成本，绝不接收 prompt 或代码内容。
"""
from __future__ import annotations

import os
from datetime import date
from typing import List, Optional

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
    args.append(limit)
    sql = f"""
        SELECT email, dept,
               SUM(total_tokens) AS total_tokens,
               SUM(cost_usd)     AS cost_usd
        FROM usage_daily
        WHERE usage_date >= current_date - ($1::int - 1)
        {where_source}
        GROUP BY email, dept
        ORDER BY total_tokens DESC
        LIMIT ${len(args)};
    """
    async with _pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return {"days": days, "source": source,
            "ranking": [dict(r) for r in rows]}


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
        ppl = await conn.fetch(f"""
            SELECT email, dept, SUM(total_tokens) t, ROUND(SUM(cost_usd),2) c,
                   SUM(total_tokens) FILTER (WHERE source='api') api,
                   SUM(total_tokens) FILTER (WHERE source='subscription') sub
            FROM usage_daily WHERE usage_date >= {window}
            GROUP BY email, dept ORDER BY t DESC LIMIT 50""", days)
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

    def fmt(n):
        return f"{int(n or 0):,}"

    sections = [
        _table("个人 Token 榜 (Top 50)", ["#", "邮箱", "部门", "Token", "其中 API", "其中订阅", "成本$"],
               [(i + 1, r["email"], r["dept"], fmt(r["t"]), fmt(r["api"]), fmt(r["sub"]), r["c"] or 0)
                for i, r in enumerate(ppl)]),
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
