#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开发用收集端（标准库 + SQLite）—— 端到端验证上报链路，免 docker/Postgres。

契约：
  POST /v1/tokscale/report  Bearer 鉴权；接收 {serial, email, hostname,
                             models:{entries:[...]}, monthly:{entries:[...]}}
                             两部分都 UPSERT（幂等，lifetime + monthly 快照）
  GET  /v1/leaderboard      按人聚合（lifetime 快照）
  GET  /v1/breakdown?by=client|client_model|client_provider_model
  GET  /v1/trend?email=...  月度时间序列
  GET  /v1/raw              明细（调试用）

主键 (email, period_type, period, source, client, provider, model)
同一主键连续 POST 两次 → UPSERT 覆盖，总量不变（不翻倍）。

部署：CentOS7 + Python 3.6.8 / macOS Python3 均可。
环境变量：COLLECTOR_API_TOKENS=devtoken  DEV_DB=/tmp/tok.db  PORT=8090
"""
import calendar
import datetime
import json
import os
import re
import sqlite3
import sys
import time
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    from http.server import ThreadingHTTPServer  # Python 3.7+
except ImportError:  # Python 3.6 (CentOS7)
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
DB = os.environ.get("DEV_DB", "/tmp/tok.db")
def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)
TOKENS = {t.strip() for t in os.environ.get("COLLECTOR_API_TOKENS", "devtoken").split(",") if t.strip()}
PORT = int(os.environ.get("PORT", "8090"))

# 载入飞连凭证，用于按序列号反解身份。
# 多候选路径：开发态 ../pipeline/.env；部署态脚本同目录 ./.env（systemd EnvironmentFile 也会注入）
_d = os.path.dirname(os.path.abspath(__file__))
for _ENV in (os.path.join(_d, "..", "pipeline", ".env"), os.path.join(_d, ".env")):
    if os.path.exists(_ENV):
        for _l in open(_ENV):
            _l = _l.strip()
            if _l and not _l.startswith("#") and "=" in _l:
                _k, _v = _l.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# AI 用接口 /v1/ai/usage 的身份归一: 传登录名(无 @)时补的公司域名。
# 与 subscriptions_sync 的 user_id@keep.com 约定一致; 可用 env(或 .env)覆盖。
# 必须在上面的 .env 载入之后求值, 否则 .env 里的 AI_EMAIL_DOMAIN 被忽略。
AI_EMAIL_DOMAIN = os.environ.get("AI_EMAIL_DOMAIN", "keep.com").strip().lstrip("@").lower()

# 飞书 AI 权益计费常量 —— 必须在 .env 加载之后取值,否则 .env 里的覆盖不生效。
PACKAGE_CNY = _env_float("FEISHU_PACKAGE_CNY", 99000)
PACKAGE_POINTS = _env_float("FEISHU_PACKAGE_POINTS", 2000000)
CNY_PER_USD = _env_float("CNY_PER_USD", 7.15)
FEISHU_USD_PER_POINT = (PACKAGE_CNY / PACKAGE_POINTS / CNY_PER_USD) if PACKAGE_POINTS and CNY_PER_USD else 0.0

_fc = None
_serial_cache = {}  # type: dict


def _resolve_serial(serial):
    """序列号 → {name, email, department}。失败/无飞连则返回空 dict。"""
    if not serial:
        return {}
    if serial in _serial_cache:
        return _serial_cache[serial]
    global _fc
    out = {}
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        if _fc is None:
            from feilian_client import FeilianClient
            _fc = FeilianClient()
        dev = _fc.device_by_serial(serial)
        if dev:
            out = {
                "name": dev.get("full_name"),
                "department": dev.get("department_name"),
                "user_id": dev.get("user_id"),
                "avatar": dev.get("icon_url") or "",
            }
            try:
                root = _fc.root_department_id()
                data = _fc._request(
                    "GET", "/api/open/v2/user/list",
                    query={"department_id": root, "fetch_child": "true",
                           "query": dev.get("full_name"), "limit": 10})
                users = (data or {}).get("user_list") or []
                # 同名串号防护：优先用设备自带的 open_id(user_id)精确命中，
                # 而非「第一个同名」——同名不同人时按名字取会归错。
                # 飞连 user/list 里 open_id 落在 'id' 字段；'user_id' 是登录名。
                dev_uid = dev.get("user_id")
                chosen = None
                if dev_uid:
                    chosen = next((u for u in users if u.get("id") == dev_uid), None)
                if chosen is None:
                    chosen = next(
                        (u for u in users if u.get("full_name") == dev.get("full_name")),
                        None)
                if chosen:
                    out["email"] = chosen.get("email")
                    # 用户档案里的部门路径比设备记录更权威/更新，命中则覆盖
                    if chosen.get("department_path"):
                        out["department"] = chosen.get("department_path")
                    if chosen.get("avatar"):
                        out["avatar"] = chosen.get("avatar")
            except Exception:
                pass
    except Exception as e:
        out = {"error": str(e)}
    _serial_cache[serial] = out
    return out


def _autofill_people_for_emails(conn, emails):
    """Best-effort email -> people backfill for explicit-email ingest paths.

    Hermes already sends the canonical company email, but the leaderboard needs
    the `people` row for Chinese name/avatar/full department.  This is optional:
    Feilian failures must never block token ingestion.
    """
    pending = []
    seen = set()
    for raw in emails or []:
        email = raw.strip() if isinstance(raw, str) else ""
        if not email or "@" not in email or email in seen:
            continue
        seen.add(email)
        row = conn.execute("SELECT name, avatar, dept FROM people WHERE email=?", (email,)).fetchone()
        if row and (row[0] or row[1]) and _to_keep(row[2]):
            continue
        pending.append(email)
    if not pending:
        return 0

    global _fc
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        if _fc is None:
            from feilian_client import FeilianClient
            _fc = FeilianClient()
        root = _fc.root_department_id()
    except Exception:
        return 0

    filled = 0
    for email in pending:
        try:
            user = _fc.user_by_email(email, root)
        except Exception:
            continue
        if not user:
            continue
        name = user.get("full_name") or ""
        avatar = user.get("avatar") or ""
        dept = user.get("department_path") or ""
        if not name or not dept:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO people(email, name, avatar, dept) VALUES(?,?,?,?)",
            (email, name, avatar, dept))
        filled += 1
    return filled


def _autofill_people_for_hermes_records(conn, records):
    emails = []
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        email = rec.get("email")
        model = rec.get("model")
        if not isinstance(email, str) or not email.strip() or not isinstance(model, str) or not model.strip():
            continue
        total = num(rec, "total_tokens", "total")
        if total <= 0:
            total = num(rec, "input_tokens", "input") + num(rec, "output_tokens", "output")
        if total > 0:
            emails.append(email)
    return _autofill_people_for_emails(conn, emails)


def _autofill_people_for_hermes_usage(conn, source, client, records):
    emails = []
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        email = rec.get("email")
        model = rec.get("model")
        if not isinstance(email, str) or not email.strip() or not isinstance(model, str) or not model.strip():
            continue
        total = num(rec, "total_tokens", "total")
        if total <= 0:
            total = num(rec, "input_tokens", "input") + num(rec, "output_tokens", "output")
        if total > 0:
            emails.append(email)
    rows = conn.execute(
        """
        SELECT DISTINCT u.email
        FROM usage u LEFT JOIN people p ON p.email = u.email
        WHERE u.source = ? AND u.client = ? AND u.period_type = 'lifetime' AND u.total > 0
          AND (p.email IS NULL OR COALESCE(p.name,'') = '' OR COALESCE(p.dept,'') = '' OR p.dept = 'unknown')
        """,
        (source, client),
    ).fetchall()
    emails.extend(r[0] for r in rows)
    return _autofill_people_for_emails(conn, emails)


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS usage(
    email        TEXT    NOT NULL,
    dept         TEXT    NOT NULL DEFAULT '',
    period_type  TEXT    NOT NULL,
    period       TEXT    NOT NULL,
    source       TEXT    NOT NULL DEFAULT 'subscription',
    client       TEXT    NOT NULL DEFAULT 'unknown',
    provider     TEXT    NOT NULL DEFAULT '',
    model        TEXT    NOT NULL DEFAULT 'unknown',
    input        INTEGER NOT NULL DEFAULT 0,
    output       INTEGER NOT NULL DEFAULT 0,
    cache_read   INTEGER NOT NULL DEFAULT 0,
    cache_write  INTEGER NOT NULL DEFAULT 0,
    reasoning    INTEGER NOT NULL DEFAULT 0,
    total        INTEGER NOT NULL DEFAULT 0,
    cost         REAL    NOT NULL DEFAULT 0,
    messages     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (email, period_type, period, source, client, provider, model)
)
"""

# 保留旧表，不破坏已有数据
_CREATE_LEGACY = """
CREATE TABLE IF NOT EXISTS usage_daily(
    email TEXT, dept TEXT, usage_date TEXT, source TEXT, tool TEXT, model TEXT,
    input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER,
    total INTEGER, cost REAL, messages INTEGER,
    PRIMARY KEY(email,usage_date,source,tool,model))
"""


def db():
    """返回已初始化的 sqlite3 连接（自动建表）。"""
    parent = os.path.dirname(os.path.abspath(DB))
    if parent:
        os.makedirs(parent, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute(_CREATE_TABLE)
    c.execute(_CREATE_LEGACY)
    # 人员档案:email → 中文姓名 + 飞连头像 + 部门(身份反解时落库,看板 join 用)
    c.execute("""CREATE TABLE IF NOT EXISTS people(
        email TEXT PRIMARY KEY, name TEXT, avatar TEXT, dept TEXT)""")
    # 上报审计:每台机器(序列号)最近一次订阅制上报的来源痕迹。
    # via='mdm'(飞连自动) / 'manual'(员工手工补报)。客户端推的订阅制数据是唯一
    # 可被伪造/出人为坏数据的来源(LiteLLM/Cursor 是服务端拉,无客户端输入),
    # 故留痕用于回溯 + 给看板打「手工」角标。INSERT OR REPLACE 只保最近一次。
    c.execute("""CREATE TABLE IF NOT EXISTS report_log(
        serial TEXT PRIMARY KEY, email TEXT, hostname TEXT, os TEXT, ip TEXT,
        via TEXT NOT NULL DEFAULT 'mdm', reported_at TEXT)""")
    _report_log_cols = {r[1] for r in c.execute("PRAGMA table_info(report_log)").fetchall()}
    if "os" not in _report_log_cols:
        c.execute("ALTER TABLE report_log ADD COLUMN os TEXT")
    c.execute("CREATE INDEX IF NOT EXISTS idx_report_log_email ON report_log(email)")
    # 离职名单:被标记离职的 email。所有「按人」聚合(个人榜/Cursor/部门榜)默认
    # 排除这些人(token 与人数都剔除);仅 ?show_departed=1 时才纳入。手工维护。
    c.execute("""CREATE TABLE IF NOT EXISTS departed(
        email TEXT PRIMARY KEY, reason TEXT, marked_at TEXT)""")
    # 1000 人规模:按 period_type 过滤是所有榜单的公共前缀,建索引避免全表扫
    c.execute("CREATE INDEX IF NOT EXISTS idx_usage_period ON usage(period_type, total DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_usage_dept ON usage(period_type, dept)")
    # 飞书 AI 权益(独立三表,单位=「点」credits,与 token 不加总)。
    # feishu_member 按天落库(主键含 usage_date),部门/个人榜按区间聚合 —— 不再是月累计死快照。
    # 迁移:旧表主键含 period_start、无 usage_date(月快照,语义不兼容),检测到就 DROP 重建,
    # 由采集器回填最近 N 天补回(孙可 2026-06-12)。
    _cols = [r[1] for r in c.execute("PRAGMA table_info(feishu_member)").fetchall()]
    if _cols and "usage_date" not in _cols:
        c.execute("DROP TABLE feishu_member")
    c.execute("""CREATE TABLE IF NOT EXISTS feishu_member(
        email TEXT NOT NULL, name TEXT DEFAULT '', dept TEXT DEFAULT 'unknown',
        feature_key TEXT NOT NULL, credits REAL NOT NULL DEFAULT 0,
        usage_date TEXT NOT NULL,
        avatar TEXT DEFAULT '', entity_id TEXT DEFAULT '',
        PRIMARY KEY(email, feature_key, usage_date))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_feishu_member_date ON feishu_member(usage_date)")
    c.execute("""CREATE TABLE IF NOT EXISTS feishu_quota(
        feature_key TEXT NOT NULL, quota REAL DEFAULT 0, used REAL DEFAULT 0,
        remain REAL DEFAULT 0, period_start TEXT NOT NULL, period_end TEXT DEFAULT '',
        PRIMARY KEY(feature_key, period_start))""")
    c.execute("""CREATE TABLE IF NOT EXISTS feishu_trend(
        usage_date TEXT NOT NULL, biz_type TEXT NOT NULL, biz_name TEXT DEFAULT '',
        credits REAL DEFAULT 0, user_count INTEGER DEFAULT 0,
        PRIMARY KEY(usage_date, biz_type))""")
    # 订阅快照表由独立同步器维护，这里只保证 dev collector 本地库必有同 schema。
    import subscriptions_sync
    subscriptions_sync.ensure_tables(c)
    c.commit()
    return c


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def num(r, *keys):
    """从 dict r 中按 keys 顺序取第一个非 None 整数，失败返回 0。"""
    for k in keys:
        if k in r and r[k] is not None:
            try:
                return int(r[k])
            except (TypeError, ValueError, OverflowError):
                try:
                    f = float(r[k])
                    if f != f or f in (float("inf"), float("-inf")):  # NaN / ±inf → 0
                        return 0
                    return int(f)
                except (TypeError, ValueError, OverflowError):
                    pass
    return 0


def _coerce_date(v):
    """datetime/date/ISO 字符串 -> date。"""
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    return datetime.datetime.strptime(str(v), "%Y-%m-%d").date()


def months_overlapped(start, end):
    """闭区间 [start,end] 触达的自然月数。

    例如 2026-06-01~2026-06-30 => 1，2026-06-30~2026-07-01 => 2。
    若 end < start，按 1 月处理，避免坏数据把订阅费算成 0 或负数。
    """
    start_d = _coerce_date(start)
    end_d = _coerce_date(end)
    if end_d < start_d:
        return 1
    return (end_d.year - start_d.year) * 12 + (end_d.month - start_d.month) + 1


def prorated_month_fraction(start, end):
    """按天摊销的「月费倍数」：闭区间 [start,end] 横跨的每个自然月，
    取「窗口落在该月的天数 / 该月总天数」之和。

    例如 6/6~6/12（含首尾 7 天，6 月 30 天）=> 7/30；
    5/14~6/12 => 18/31 + 12/30；整月 6/1~6/30 => 30/30 = 恰好 1.0。
    end < start（坏数据）时回退为 1.0，与 months_overlapped 的「至少 1 月」语义一致，
    防止订阅费被算成 0 或负数。
    """
    start_d = _coerce_date(start)
    end_d = _coerce_date(end)
    if end_d < start_d:
        return 1.0
    fraction = 0.0
    year, month = start_d.year, start_d.month
    while (year, month) <= (end_d.year, end_d.month):
        days_in_month = calendar.monthrange(year, month)[1]
        month_first = datetime.date(year, month, 1)
        month_last = datetime.date(year, month, days_in_month)
        seg_start = start_d if start_d > month_first else month_first
        seg_end = end_d if end_d < month_last else month_last
        days_in_window = (seg_end - seg_start).days + 1
        fraction += days_in_window / days_in_month
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return fraction


def _interval_fraction(win_start, win_end, sub_start, sub_end):
    """窗口 [win] 与订阅区间 [sub] 的重叠月费倍数；无重叠返回 0。"""
    ws = _coerce_date(win_start)
    we = _coerce_date(win_end)
    ss = _coerce_date(sub_start) if sub_start else ws
    se = _coerce_date(sub_end) if sub_end else we
    eff_s = max(ws, ss)
    eff_e = min(we, se)
    if eff_s > eff_e:
        return 0.0
    return prorated_month_fraction(eff_s, eff_e)


def _window_dates(qs):
    """qs → (win_start, win_end) date 对；无窗口参数(lifetime)返回 None。"""
    frm = (qs.get("from") or [None])[0]
    to = (qs.get("to") or [None])[0]
    today = datetime.date.today()
    if frm or to:
        ws = _coerce_date(frm) if frm else datetime.date(1970, 1, 1)
        we = _coerce_date(to) if to else today
        return ws, we
    raw = (qs.get("days") or [None])[0]
    try:
        days = int(raw) if raw not in (None, "", "all") else None
    except (TypeError, ValueError):
        days = None
    if days and days > 0:
        return today - datetime.timedelta(days=days - 1), today
    return None


def _interval_overlaps(win_start, win_end, sub_start, sub_end):
    ws = _coerce_date(win_start)
    we = _coerce_date(win_end)
    ss = _coerce_date(sub_start) if sub_start else ws
    se = _coerce_date(sub_end) if sub_end else we
    return max(ws, ss) <= min(we, se)


def load_subscriptions(conn):
    """subscriptions 表(一席一行) -> {email: [seat_row, ...]}。

    seat_row = {tool, tier, fee, start, end}（start/end 可为 None=无界）。
    表不存在/为空时返回空 dict，便于旧库或局部测试直接复用。
    对外(API payload)按工具聚合用 _group_subs()。
    """
    try:
        rows = conn.execute(
            "SELECT email, tool, tier, monthly_fee_usd, start_date, end_date"
            " FROM subscriptions ORDER BY email, tool, seat"
        ).fetchall()
    except Exception:
        return {}
    out = {}
    for r in rows:
        email = r[0] or ""
        if not email:
            continue
        out.setdefault(email, []).append({
            "tool": (r[1] or "").lower(),
            "tier": r[2] or "standard",
            "fee": float(r[3] or 0),
            "start": r[4] or None,
            "end": r[5] or None,
        })
    return out


_TIER_RANK = {"premium": 2, "standard": 1}


def _group_subs(seat_rows):
    """席位行 → 按工具聚合的徽章 payload。调用方先做窗口重叠过滤。

    每工具一个徽章：fee=在窗席位月费之和，seats=在窗席位数，tier=最高席别；
    start 仅当所有席位都有开通日时取最早；end 仅当所有席位都已删除时取最晚
    （任一席仍活跃 → 不带 end，前端不灰显）。
    """
    grouped = {}
    for s in seat_rows:
        tool = s.get("tool") or ""
        g = grouped.get(tool)
        if g is None:
            g = {"tool": tool, "tier": s.get("tier") or "standard",
                 "fee": 0.0, "seats": 0, "_starts": [], "_ends": []}
            grouped[tool] = g
        g["fee"] += float(s.get("fee") or 0)
        g["seats"] += 1
        if _TIER_RANK.get(s.get("tier") or "standard", 0) > _TIER_RANK.get(g["tier"], 0):
            g["tier"] = s.get("tier")
        g["_starts"].append(s.get("start"))
        g["_ends"].append(s.get("end"))
    out = []
    for tool in sorted(grouped):
        g = grouped[tool]
        starts = g.pop("_starts")
        ends = g.pop("_ends")
        g["fee"] = round(g["fee"], 4)
        if starts and all(starts):
            g["start"] = min(starts)
        if ends and all(ends):
            g["end"] = max(ends)
        out.append(g)
    return out


_CLIENT_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex CLI",
    "gemini": "Gemini CLI",
    "cursor": "Cursor",
    "opencode": "OpenCode",
    "kimi": "Kimi CLI",
}
_CLIENT_TO_SUB_TOOL = {
    "Claude Code": "claude",
    "Codex CLI": "codex",
    "Cursor": "cursor",
}


def _single_tool_subs(subs_by_email, email, tool, win_start=None, win_end=None):
    # 工具榜只挂同工具的订阅徽标(多席位聚合成一个),复用个人榜 subs 结构避免前端分叉。
    if not tool:
        return []
    kept = []
    for sub in subs_by_email.get(email, []):
        if (sub.get("tool") or "").lower() != tool:
            continue
        if win_start is not None and win_end is not None and not _interval_overlaps(
                win_start, win_end, sub.get("start"), sub.get("end")):
            continue
        kept.append(sub)
    return _group_subs(kept)

# 用 INSERT OR REPLACE 而非 ON CONFLICT DO UPDATE：
# 后者需 SQLite ≥3.24，而部署目标(CentOS7)是 3.7.17。
# 提供全部 16 列，主键冲突时整行替换 —— 等价覆盖，去重语义不变(不翻倍)。
_UPSERT_SQL = """
INSERT OR REPLACE INTO usage
    (email, dept, period_type, period, source, client, provider, model,
     input, output, cache_read, cache_write, reasoning, total, cost, messages)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def _upsert_lifetime(conn, email, dept, entries):
    """将 tokscale models --json entries UPSERT 为 period_type=lifetime。"""
    up = 0
    for e in entries:
        client_raw = e.get("client", "unknown")
        client = _CLIENT_LABELS.get(client_raw, client_raw)
        provider = e.get("provider") or ""
        model = e.get("model") or "unknown"
        inp = num(e, "input")
        out = num(e, "output")
        cr = num(e, "cacheRead")
        cw = num(e, "cacheWrite")
        reasoning = num(e, "reasoning")
        total = inp + out + cr + cw + reasoning
        cost = float(e.get("cost") or 0)
        messages = num(e, "messageCount")
        conn.execute(_UPSERT_SQL, (
            email, dept, "lifetime", "all", "subscription",
            client, provider, model,
            inp, out, cr, cw, reasoning, total, cost, messages,
        ))
        up += 1
    return up


def _upsert_monthly(conn, email, dept, entries):
    """将 tokscale monthly --json entries UPSERT 为 period_type=month。

    monthly 格式: {month, models(list), input, output, cacheRead, cacheWrite,
                   messageCount, cost}
    无 provider/reasoning/client — 存为空字符串/0，client 固定 '__monthly__'。
    """
    up = 0
    for e in entries:
        month = e.get("month") or ""
        if not month:
            continue
        inp = num(e, "input")
        out = num(e, "output")
        cr = num(e, "cacheRead")
        cw = num(e, "cacheWrite")
        reasoning = num(e, "reasoning")          # monthly 通常无此字段 → 0
        total = inp + out + cr + cw + reasoning
        cost = float(e.get("cost") or 0)
        messages = num(e, "messageCount")
        # provider 必须用稳定常量：之前塞乱序模型列表 → 每次跑主键都不同 → 月度翻倍。
        # 月度只做时间桶,模型维度从 lifetime 行取,这里 provider 固定为空。
        conn.execute(_UPSERT_SQL, (
            email, dept, "month", month, "subscription",
            "__monthly__", "", "__aggregated__",
            inp, out, cr, cw, reasoning, total, cost, messages,
        ))
        up += 1
    return up


def _upsert_daily(conn, email, dept, graph):
    """将 tokscale graph 的 contributions[] 落为 period_type='day' 日桶(每天每模型 token)。
    graph: {contributions:[{date:'YYYY-MM-DD', clients:[{client,modelId,providerId,
            tokens:{input,output,cacheRead,cacheWrite,reasoning}, cost, messages}]}]}"""
    up = 0
    for d in (graph or {}).get("contributions") or []:
        day = d.get("date")
        if not day:
            continue
        for c in d.get("clients") or []:
            tk = c.get("tokens") or {}
            client_raw = c.get("client", "unknown")
            client = _CLIENT_LABELS.get(client_raw, client_raw)
            inp = num(tk, "input"); out = num(tk, "output")
            cr = num(tk, "cacheRead"); cw = num(tk, "cacheWrite"); rs = num(tk, "reasoning")
            total = inp + out + cr + cw + rs
            conn.execute(_UPSERT_SQL, (
                email, dept, "day", day, "subscription",
                client, c.get("providerId") or "", c.get("modelId") or "unknown",
                inp, out, cr, cw, rs, total, float(c.get("cost") or 0), num(c, "messages"),
            ))
            up += 1
    return up


# ---------------------------------------------------------------------------
# Hermes 显式-email 上报（additive，不动现有 tokscale/feishu 路径）
# ---------------------------------------------------------------------------
# source 白名单：只接受这些 source 写库（防止任意外部 source 污染表 / 误删别的来源）。
# 经环境变量 HERMES_REPORT_SOURCES 可扩充（逗号分隔），默认仅 'hermes'。
HERMES_ALLOWED_SOURCES = {
    s.strip() for s in os.environ.get("HERMES_REPORT_SOURCES", "hermes").split(",")
    if s.strip()
}


def _upsert_hermes_usage(conn, source, client, date, records):
    """把一批「该 date 的当日累计快照」record 写进 usage 表的 day/month/lifetime 三桶。

    幂等口径（与 litellm_collector 同思路：DELETE 旧行后整批重写，连跑同日不翻倍）：
      - day 桶是唯一真相。先 DELETE 掉本 source 在该 date 的全部 day 行（严格限定
        source=本次请求体的 source，绝不误删别的 source），再把本批 record 逐行 INSERT
        为 period=date 的 day 行。
      - month / lifetime 桶不直接收数，而是「重算」：DELETE 本 source 的全部 month +
        lifetime 行，再从「本 source 现存的所有 day 行」按 (email,dept,client,provider,
        model) 维度 SUM 重新聚合写入。month 按 period=YYYY-MM 求和，lifetime period='all'
        全量求和。因此无论某天被上报几次、补报几天，month/lifetime 永远等于 day 行之和，
        结构上不可能翻倍。

    返回 (written, skipped)。缺 email 或非法 record 跳过并计数，不抛异常。
    """
    written = 0
    skipped = 0
    # ---- 先在「本批内」按 (email, provider, model) 求和，再写库 ----
    # day 桶 PK 含 (email, period, source, client, provider, model)，用 INSERT OR REPLACE。
    # 若同一批里两条 record 撞同一把 key，逐条 REPLACE 会只保留最后一条（丢量）。所以这里
    # 先聚合求和，保证「一把 key 一行、值是批内之和」——与「多个智能体累加到本人」一致，
    # 也不依赖上游 uploader 是否已去重。dept 取该 key 第一条非空值。
    # 身份字段(email/model)必须是「非空字符串」才算有效：
    #   - 非字符串(如 email:123 / model:{…}) → 当坏记录跳过计数，绝不强转成 "123" 之类
    #     的假身份混进榜，也绝不抛异常把整请求打成 500（SPEC：坏记录 skip+count，不 fail）。
    def _opt_str(rec, k):
        v = rec.get(k)
        return v.strip() if isinstance(v, str) else None

    agg = {}            # (email, provider, model) -> {dept, inp, out, total}
    for rec in records or []:
        if not isinstance(rec, dict):
            skipped += 1
            continue
        email = _opt_str(rec, "email")
        model = _opt_str(rec, "model")
        if not email or not model:          # 缺/非字符串 email/model → 无法可靠归属，跳过计数
            skipped += 1
            continue
        inp = num(rec, "input_tokens", "input")
        out = num(rec, "output_tokens", "output")
        total = num(rec, "total_tokens", "total")
        if total <= 0:                       # total 缺省 = input + output
            total = inp + out
        if total <= 0:                       # 无任何可计量 token → 跳过，绝不拿 0 覆盖好数据
            skipped += 1
            continue
        provider = _opt_str(rec, "provider") or ""     # 非字符串/缺 → ''
        dept = _opt_str(rec, "dept") or "unknown"      # 非字符串/缺 → 'unknown'
        key = (email, provider, model)
        slot = agg.get(key)
        if slot is None:
            agg[key] = {"dept": dept, "inp": inp, "out": out, "total": total}
        else:
            slot["inp"] += inp
            slot["out"] += out
            slot["total"] += total
    # 区分两种「无有效记录」：
    #   - 权威空快照（请求体 records 本就为 []）→ 照常 DELETE 清空该日，遵守 snapshot 契约
    #     （表示「该日确无 Hermes 用量」，应清掉可能残留的旧行）。
    #   - 有记录但全被跳过（坏输入/解析不到）→ 守卫：不碰库，保留上次的好快照，
    #     绝不让一批坏输入把好数据擦成空。
    if not agg and records:
        return 0, skipped
    # ---- day 桶：DELETE 本 source 在该 date 的旧行，再写当天聚合快照 ----
    conn.execute(
        "DELETE FROM usage WHERE source=? AND period_type=? AND period=?",
        (source, "day", date))
    for (email, provider, model), v in agg.items():
        conn.execute(_UPSERT_SQL, (
            email, v["dept"], "day", date, source,
            client, provider, model,
            v["inp"], v["out"], 0, 0, 0, v["total"], 0.0, 0,
        ))
        written += 1
    # ---- month / lifetime 桶：DELETE 本 source 全部，再从 day 行重算（防翻倍）----
    conn.execute(
        "DELETE FROM usage WHERE source=? AND period_type IN ('month','lifetime')",
        (source,))
    # month：按 YYYY-MM 求和（period 取 day-period 的前 7 位）
    conn.execute("""
        INSERT INTO usage
            (email, dept, period_type, period, source, client, provider, model,
             input, output, cache_read, cache_write, reasoning, total, cost, messages)
        SELECT email, MAX(dept), 'month', substr(period,1,7), source, client, provider, model,
               SUM(input), SUM(output), 0, 0, 0, SUM(total), 0, 0
        FROM usage
        WHERE source=? AND period_type='day'
        GROUP BY email, period_type, substr(period,1,7), source, client, provider, model
    """, (source,))
    # lifetime：全量求和，period='all'
    conn.execute("""
        INSERT INTO usage
            (email, dept, period_type, period, source, client, provider, model,
             input, output, cache_read, cache_write, reasoning, total, cost, messages)
        SELECT email, MAX(dept), 'lifetime', 'all', source, client, provider, model,
               SUM(input), SUM(output), 0, 0, 0, SUM(total), 0, 0
        FROM usage
        WHERE source=? AND period_type='day'
        GROUP BY email, source, client, provider, model
    """, (source,))
    return written, skipped


def _range_clause(qs, prefix=""):
    """全局时间范围 → (where_sql, params)。优先级:
      ?from=YYYY-MM-DD&to=YYYY-MM-DD  → 日桶在 [from,to] 内求和(Kibana 式起止日期)
      ?days=N                          → 日桶近 N 天(快捷)
      无                                → lifetime 全部
    """
    p = prefix
    frm = (qs.get("from") or [None])[0]
    to = (qs.get("to") or [None])[0]
    if frm or to:
        conds = ["%speriod_type='day'" % p]
        params = []
        if frm:
            conds.append("%speriod >= ?" % p); params.append(frm)
        if to:
            conds.append("%speriod <= ?" % p); params.append(to)
        return (" AND ".join(conds), params)
    raw = (qs.get("days") or [None])[0]
    try:
        days = int(raw) if raw not in (None, "", "all") else None
    except (TypeError, ValueError):
        days = None
    if days and days > 0:
        cutoff = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
        return ("%speriod_type='day' AND %speriod >= ?" % (p, p), [cutoff])
    return ("%speriod_type='lifetime'" % p, [])


def _cost_window(qs):
    """个人榜订阅费的全局窗口。

    显式 from/to 或 days=N 时直接给出闭区间；默认 lifetime/all 返回 (None, None)，
    由个人榜按「每个人最早 usage.day → today」补齐。纯订阅且无 usage 的人默认算 1 个月。
    """
    today = datetime.date.today()
    frm = (qs.get("from") or [None])[0]
    to = (qs.get("to") or [None])[0]
    if frm or to:
        start = _coerce_date(frm or to or today)
        end = _coerce_date(to or frm or today)
        if frm and not to:
            end = today
        return start, end
    raw = (qs.get("days") or [None])[0]
    try:
        days = int(raw) if raw not in (None, "", "all") else None
    except (TypeError, ValueError):
        days = None
    if days and days > 0:
        return today - datetime.timedelta(days=days - 1), today
    return None, None


def _show_departed(qs):
    """解析 ?show_departed=1 → bool。1/true/yes 视为 True，其余 False。"""
    raw = (qs.get("show_departed") or [None])[0]
    return str(raw).strip().lower() in ("1", "true", "yes")


def _feishu_range(qs):
    """飞书 feishu_member.usage_date 区间 → (sql_fragment, params)。语义同 _range_clause:
    ?from=&to= 优先,否则 ?days=N,默认近 30 天。fragment 形如 ' AND usage_date >= ?',
    可直接拼在 'WHERE 1=1' 之后。统一一处,防止 _feishu/_teams/_leaderboard 三处漂移
    (2026-06-12 _teams 漏改 period_start 致全看板 500 的教训)。"""
    frm = (qs.get("from") or [None])[0]
    to = (qs.get("to") or [None])[0]
    raw = (qs.get("days") or [None])[0]
    conds, params = [], []
    if frm or to:
        if frm:
            conds.append("usage_date >= ?"); params.append(frm)
        if to:
            conds.append("usage_date <= ?"); params.append(to)
    else:
        try:
            days = int(raw) if raw not in (None, "", "all") else 30
        except (TypeError, ValueError):
            days = 30
        if days and days > 0:
            cutoff = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
            conds.append("usage_date >= ?"); params.append(cutoff)
    return ((" AND " + " AND ".join(conds)) if conds else ""), params


def _departed_set(conn):
    """departed 表里的全部 email → set(小写无关，按存入原样)。一次查询，按行判定用。"""
    try:
        return {r[0] for r in conn.execute("SELECT email FROM departed").fetchall()}
    except Exception:
        return set()


def _departed_filter(show_departed, prefix=""):
    """按人聚合的离职过滤子句。show_departed=True → 空串(不过滤);
    否则 → 'AND <prefix>email NOT IN (SELECT email FROM departed)'。"""
    if show_departed:
        return ""
    return " AND %semail NOT IN (SELECT email FROM departed)" % prefix


def _ancestors(path):
    """完整部门路径 → 该路径及其所有祖先路径(含自身),用于层级 roll-up。
    'Keep/A/B' → ['Keep','Keep/A','Keep/A/B']；无 '/' → [path]；空 → []。
    这样把叶子组(IT 组)的用量/人数累加到其每一级父部门(基础技术部、技术平台部)。"""
    if not path:
        return []
    segs = path.split("/")
    return ["/".join(segs[:i]) for i in range(1, len(segs) + 1)]


_SP_RE = re.compile(r"\(SP\d+\)")  # 供应商公司名带的 (SP000083) 标记 → 外部合作商判定


def _normalize_dept_path(path):
    """部门路径归一化。两类外包分流：
    1) 外部供应商(合作商-W / 任一段带 (SP数字) / 裸公司名)：收口到 'Keep/外部合作商/<公司名(SP码)>'
       —— 部门榜聚成单个父节点，下钻见各公司，不平铺(孙可 2026-06-11「很乱」)。
       'Keep/合作商/W/北京再作品牌管理有限公司(SP000083)' → 'Keep/外部合作商/北京再作品牌管理有限公司(SP000083)'
       裸名 '四川乔木禾电子商务有限公司(SP000442)' → 'Keep/外部合作商/四川乔木禾电子商务有限公司(SP000442)'
    2) 业务外包-V(真实部门)：剥 合作商/供应商 前缀，叶子按 '-' 拆层级，折回真实 Keep 树。
       'Keep/合作商/V/技术平台部-基础技术部-安全组' → 'Keep/技术平台部/基础技术部/安全组'。
       叶子短横各段须与飞连真实部门名逐字一致，roll-up 才能与 headcount 正确合并。
    非外包路径原样返回(幂等)。空/None 原样返回。"""
    if not path:
        return path
    segs = [s for s in path.split("/") if s]
    if not segs:
        return path
    root = segs[0] if segs[0] == "Keep" else "Keep"  # 裸公司名也归 Keep 树
    # 1) 外部供应商：任一段带 (SP数字) → 取该公司名段，收口到 外部合作商
    sp_seg = next((s for s in segs if _SP_RE.search(s)), None)
    if sp_seg:
        return root + "/外部合作商/" + sp_seg
    if "合作商" not in segs:
        return path
    i = segs.index("合作商")
    head = segs[:i] or ["Keep"]   # 真实前缀(通常 'Keep')
    rest = segs[i + 1:]           # 供应商代号 + 叶子段
    # 2) 合作商-W(供应商，无 SP 码也兜底归外部)：vendor code 'W' → 外部合作商
    if rest and rest[0] == "W":
        company = rest[-1] if len(rest) > 1 else "未知供应商"
        return "/".join(head) + "/外部合作商/" + company
    # 3) 合作商-V(真实部门)：丢供应商代号，叶子拆短横折回真实树
    if rest:
        rest = rest[1:]
    expanded = []
    for seg in rest:
        expanded.extend(p for p in seg.split("-") if p)
    out = head + expanded
    return "/".join(out) if out else path


def _to_keep(raw):
    """任意 dept 字符串 → 归一化后的 Keep 路径；归不到 Keep 树(裸非 SP 组名/空/unknown)→ None。
    裸供应商公司名(带 SP 码)经 _normalize_dept_path 会变 'Keep/外部合作商/...'，故能被收口；
    纯飞书裸组名('品质组')归一后仍无 Keep 前缀 → None → 由上层落未归类。"""
    if not raw:
        return None
    n = _normalize_dept_path(raw)
    return n if n and n.startswith("Keep") else None


# ---------------------------------------------------------------------------
# 飞连部门总人数缓存（部门榜 headcount / active_rate 用）
# ---------------------------------------------------------------------------
_DEPT_HEADCOUNT_FILE = os.path.join(os.path.dirname(os.path.abspath(DB)), "dept_headcount.json")
_DEPT_HEADCOUNT_TTL = 6 * 3600  # 6 小时
_dept_headcount_mem = None  # 进程内一次性缓存，避免每请求读盘


def _fetch_dept_headcount():
    """飞连一次性分页拉全量在职用户，按完整 department_path 精确分组计数。
    user/list department_id=root&fetch_child=true&status=0(在职)&limit=200&offset 翻页。
    返回 {department_path: 人数}。任何异常 → 抛出，由上层 graceful 处理。"""
    from feilian_client import FeilianClient
    fc = FeilianClient()
    root = fc.root_department_id()
    counts = {}
    off = 0
    while True:
        data = fc._request(
            "GET", "/api/open/v2/user/list",
            query={"department_id": root, "fetch_child": "true",
                   "status": 0, "limit": 200, "offset": off})
        ul = (data or {}).get("user_list") or []
        for u in ul:
            path = u.get("department_path")
            if path:
                path = _normalize_dept_path(path)  # 外包归并:合作商路径折回真实部门
                counts[path] = counts.get(path, 0) + 1
        off += len(ul)
        total = (data or {}).get("count") or 0
        if len(ul) < 200 or off >= total:
            break
    return counts


def _dept_headcount_map():
    """部门完整路径 → 在职总人数。带 6h 文件缓存(DB 同目录 dept_headcount.json)。
    懒加载、graceful：任何飞连/IO 异常返回空 dict，绝不让 _teams 报错。"""
    global _dept_headcount_mem
    if _dept_headcount_mem is not None:
        return _dept_headcount_mem
    now = time.time()
    # 1) 文件缓存命中且未过期 → 直接用
    try:
        if os.path.exists(_DEPT_HEADCOUNT_FILE):
            with open(_DEPT_HEADCOUNT_FILE) as f:
                cached = json.load(f)
            ts = float(cached.get("ts") or 0)
            counts = cached.get("counts") or {}
            if counts and (now - ts) < _DEPT_HEADCOUNT_TTL:
                _dept_headcount_mem = counts
                return counts
    except Exception:
        cached = None  # 缓存损坏，往下走重建
    else:
        cached = None
    # 2) 过期/缺失 → 飞连重建，写盘
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        counts = _fetch_dept_headcount()
        try:
            with open(_DEPT_HEADCOUNT_FILE, "w") as f:
                json.dump({"ts": now, "counts": counts}, f, ensure_ascii=False)
        except Exception:
            pass
        _dept_headcount_mem = counts
        return counts
    except Exception:
        # 飞连失败：若有旧缓存(即便过期)兜底好过空;否则空 dict
        try:
            if os.path.exists(_DEPT_HEADCOUNT_FILE):
                with open(_DEPT_HEADCOUNT_FILE) as f:
                    stale = (json.load(f) or {}).get("counts") or {}
                if stale:
                    _dept_headcount_mem = stale
                    return stale
        except Exception:
            pass
        return {}


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json;charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return auth.split(" ", 1)[1] in TOKENS

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    # ------------------------------------------------------------------
    def _send_local(self, filename, content_type):
        """读取本脚本同目录下的文件原样返回(看板/说明页/补报脚本共用)。"""
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        try:
            body = open(p, "rb").read()
        except OSError:
            return self._send(404, {"error": filename + " not found"})
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_script_file(self, local_name, fallback_relpath):
        here = os.path.dirname(os.path.abspath(__file__))
        local = os.path.join(here, local_name)
        repo = os.path.normpath(os.path.join(os.path.dirname(here), fallback_relpath))
        p = local if os.path.exists(local) else repo
        try:
            return open(p, "rb").read()
        except OSError:
            return None

    def _send_script_file(self, local_name, fallback_relpath, content_type):
        body = self._read_script_file(local_name, fallback_relpath)
        if body is None:
            return self._send(404, {"error": local_name + " not found"})
        self._send_bytes(body, content_type)

    @staticmethod
    def _shell_dq(value):
        return str(value).replace("\\", "\\\\").replace("$", "\\$").replace('"', '\\"').replace("`", "\\`")

    def _public_base_url(self):
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
        if not host:
            host = "127.0.0.1:%s" % PORT
        return "%s://%s" % (proto, host)

    def _dashboard(self):
        """提供中性企业实时看板(同目录 dashboard.html,前端 fetch /v1/* 同源)。"""
        self._send_local("dashboard.html", "text/html;charset=utf-8")

    def _help(self):
        """数据说明页:数据来源 / 刷新周期 / MDM 失败时如何手工补报。"""
        self._send_local("help.html", "text/html;charset=utf-8")

    def _tokreport_sh(self):
        """手工补报脚本(与飞连 MDM 下发的同一份)。员工 `sudo bash` 运行即可，
        按序列号经飞连反解身份，机器侧零配置。仅内网可达。"""
        body = self._read_script_file("remote_tokscale_report.sh",
                                      os.path.join("agent", "remote_tokscale_report.sh"))
        if body is None:
            return self._send(404, {"error": "tokreport.sh not found"})
        text = body.decode("utf-8")
        token = sorted(TOKENS)[0] if TOKENS else ""
        text = text.replace(
            'COLLECTOR="${COLLECTOR:-https://collector.example.com}"',
            'COLLECTOR="${COLLECTOR:-%s}"' % self._shell_dq(self._public_base_url()),
        )
        text = text.replace(
            'TOKEN="${TOKEN:-}"',
            'TOKEN="${TOKEN:-%s}"' % self._shell_dq(token),
        )
        self._send_bytes(text.encode("utf-8"), "text/x-shellscript;charset=utf-8")

    def _tokreport_ps1(self):
        """Windows 手工补报脚本。MDM 使用独立的 mdm_bootstrap_windows.ps1；
        这里仅提供它下载/手工补报用的 reporter 源码。"""
        self._send_script_file("tokreport.ps1", os.path.join("agent", "tokreport_windows.ps1"),
                               "text/plain;charset=utf-8")

    _CT = {".otf": "font/otf", ".woff2": "font/woff2", ".woff": "font/woff",
           ".ttf": "font/ttf", ".css": "text/css;charset=utf-8", ".svg": "image/svg+xml",
           ".png": "image/png", ".jpg": "image/jpeg"}

    def _static(self, rel):
        """提供 /assets/* 静态资源(iconfont assets),带目录穿越保护。"""
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
        target = os.path.normpath(os.path.join(base, rel))
        if not target.startswith(base + os.sep):
            return self._send(403, {"error": "forbidden"})
        ext = os.path.splitext(target)[1].lower()
        if ext not in self._CT or not os.path.isfile(target):
            return self._send(404, {"error": "not found"})
        body = open(target, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", self._CT[ext])
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # 加固:绝不让单个上报异常把 handler 线程打挂、连接重置(那样 nginx 回 502)。
        # 坏 JSON body(json.loads 抛 ValueError/JSONDecodeError)→ 400 客户端错误;
        # 其它异常(DB/服务端 bug)→ 500,保留运维信号、不被误判成客户端问题。
        try:
            if self.path.startswith("/v1/tokscale/report"):
                return self._tokscale_report()
            if self.path.startswith("/v1/feishu/report"):
                return self._feishu_report()
            if self.path.startswith("/v1/usage/report"):
                return self._usage_report()
            self._send(404, {"error": "not found"})
        except ValueError as e:   # json.JSONDecodeError 是 ValueError 子类
            sys.stderr.write("do_POST %s bad-json: %s\n" % (self.path, repr(e)[:300]))
            try:
                self._send(400, {"ok": False, "error": "bad request", "detail": str(e)[:200]})
            except Exception:
                pass
        except Exception as e:
            sys.stderr.write("do_POST %s server-error: %s\n" % (self.path, repr(e)[:300]))
            try:
                self._send(500, {"ok": False, "error": "internal error"})
            except Exception:
                pass

    def _feishu_report(self):
        """接收飞书 AI 权益采集器上报(独立三表,单位=点,不并入 token 榜)。
        payload: {period_start, period_end, members:[{email,name,dept,avatar,entity_id,
                  feature_key,credits}], quota:[{feature_key,quota,used,remain}],
                  trend:[{usage_date,biz_type,biz_name,credits,user_count}]}
        幂等:INSERT OR REPLACE 按主键覆盖(同周期重跑不翻倍)。"""
        if not self._auth():
            return self._send(403, {"error": "invalid token"})
        p = self._read_body()
        ps = p.get("period_start") or ""
        pe = p.get("period_end") or ""
        conn = db()
        nm = nq = nt = 0
        for m in p.get("members") or []:
            ud = m.get("usage_date") or ""
            if not ud:                       # 按天上报必须带 usage_date,缺了跳过不写脏
                continue
            conn.execute(
                "INSERT OR REPLACE INTO feishu_member"
                "(email,name,dept,feature_key,credits,usage_date,avatar,entity_id)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (m.get("email") or "", m.get("name") or "", m.get("dept") or "unknown",
                 m.get("feature_key") or "", float(m.get("credits") or 0), ud,
                 m.get("avatar") or "", m.get("entity_id") or "")); nm += 1
        for q in p.get("quota") or []:
            conn.execute(
                "INSERT OR REPLACE INTO feishu_quota"
                "(feature_key,quota,used,remain,period_start,period_end) VALUES(?,?,?,?,?,?)",
                (q.get("feature_key") or "", float(q.get("quota") or 0), float(q.get("used") or 0),
                 float(q.get("remain") or 0), ps, pe)); nq += 1
        for t in p.get("trend") or []:
            conn.execute(
                "INSERT OR REPLACE INTO feishu_trend"
                "(usage_date,biz_type,biz_name,credits,user_count) VALUES(?,?,?,?,?)",
                (t.get("usage_date") or "", str(t.get("biz_type") or ""), t.get("biz_name") or "",
                 float(t.get("credits") or 0), int(t.get("user_count") or 0))); nt += 1
        conn.commit(); conn.close()
        self._send(200, {"ok": True, "members": nm, "quota": nq, "trend": nt})

    def _usage_report(self):
        """显式-email 用量上报(additive)。给异地宿主(如 hermes-1)走 HTTP 把每人 token
        用量并入排行榜的 usage 表 —— 现有 /v1/tokscale/report 按序列号反解身份且 source
        硬编码 'subscription',收不了显式 email + 自定义 source,故新增本端点。

        payload: {source:'hermes', client:'Hermes', date:'YYYY-MM-DD',
                  records:[{email, dept, provider, model,
                            input_tokens, output_tokens, total_tokens}, ...]}
        行为:
          - source 必须在白名单(HERMES_ALLOWED_SOURCES,默认 {'hermes'})。不在 → 400 拒绝、不写库。
          - date 缺/格式不对 → 400(没有归属周期无法写桶)。
          - 写 usage 表 day/month/lifetime 三桶(口径见 _upsert_hermes_usage):day=当天快照、
            month/lifetime 从 day 行重算,连跑同日不翻倍。
          - 单条 record 缺 email/非法 → 跳过计数,返回 {ok,written,skipped},不 500。
        幂等:DELETE(限定 source=请求体值)+ 重写,严格不误删别的 source。
        """
        if not self._auth():
            return self._send(403, {"error": "invalid token"})
        p = self._read_body()
        source = (p.get("source") or "").strip()
        if source not in HERMES_ALLOWED_SOURCES:
            # 白名单外一律拒绝且不写,避免任意 source 污染表 / 误删既有来源
            return self._send(400, {
                "ok": False, "error": "source not allowed",
                "source": source, "allowed": sorted(HERMES_ALLOWED_SOURCES)})
        client = (p.get("client") or "Hermes").strip() or "Hermes"   # 缺省 'Hermes'
        # 标签归一:历史上有上报端发小写 'hermes',与官方 'Hermes' 在榜单上裂成两个工具。
        if client.lower() == "hermes":
            client = "Hermes"
        date = (p.get("date") or "").strip()
        try:
            # 真校验 YYYY-MM-DD（拒绝 2026-13-40 / 非数字等），date 进 SQL period 列。
            if date != datetime.datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d"):
                raise ValueError
        except ValueError:
            return self._send(400, {"ok": False, "error": "bad date (want YYYY-MM-DD)", "date": date})
        records = p.get("records") or []
        conn = db()
        written, skipped = _upsert_hermes_usage(conn, source, client, date, records)
        conn.commit()
        conn.close()
        people_filled = 0
        fill_conn = None
        try:
            fill_conn = db()
            people_filled = _autofill_people_for_hermes_usage(fill_conn, source, client, records)
            fill_conn.commit()
        except Exception:
            people_filled = 0
        finally:
            if fill_conn is not None:
                try:
                    fill_conn.close()
                except Exception:
                    pass
        self._send(200, {"ok": True, "written": written, "skipped": skipped,
                         "people_filled": people_filled})

    def _tokscale_report(self):
        """接收 {serial, email, hostname, models:{entries:[...]}, monthly:{entries:[...]}}
        两部分分别 UPSERT 为 lifetime / month 快照。幂等：同主键覆盖不累加。
        """
        if not self._auth():
            return self._send(403, {"error": "invalid token"})

        p = self._read_body()
        serial = p.get("serial", "")
        lifetime_entries = (p.get("models") or {}).get("entries") or []
        monthly_entries = (p.get("monthly") or {}).get("entries") or []

        # 服务端用序列号经飞连反解身份（机器侧零配置）
        ident = _resolve_serial(serial)
        email = ident.get("email") or p.get("email") or ("sn:" + serial)
        dept = ident.get("department") or "unknown"
        # 上报来源:仅接受 mdm / manual,其它一律按 mdm(老客户端不带 via 时也是 mdm)
        via = p.get("via") if p.get("via") in ("mdm", "manual") else "mdm"

        conn = db()
        up_lt = _upsert_lifetime(conn, email, dept, lifetime_entries)
        up_mo = _upsert_monthly(conn, email, dept, monthly_entries)
        up_dy = _upsert_daily(conn, email, dept, p.get("graph") or {})
        # 落人员档案:中文姓名 + 飞连头像 + 完整部门路径（看板 join 用）
        conn.execute(
            "INSERT OR REPLACE INTO people(email, name, avatar, dept) VALUES(?,?,?,?)",
            (email, ident.get("name") or email.split("@")[0],
             ident.get("avatar") or "", dept))
        # 上报审计:记这台机器最近一次上报的来源/主机/IP/时间(回溯坏数据用)
        conn.execute(
            "INSERT OR REPLACE INTO report_log(serial,email,hostname,os,ip,via,reported_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (serial, email, p.get("hostname") or "", p.get("os") or "", p.get("ip") or "", via,
             datetime.datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        conn.close()

        self._send(200, {
            "ok": True,
            "attributed_to": email,
            "dept": dept,
            "via": via,
            "upserted_lifetime": up_lt,
            "upserted_monthly": up_mo,
            "upserted_daily": up_dy,
        })

    # ------------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or path == "/dashboard" or path == "/index.html":
            return self._dashboard()
        if path == "/help" or path == "/about":
            return self._help()
        if path == "/tokreport.sh":
            return self._tokreport_sh()
        if path == "/tokreport.ps1":
            return self._tokreport_ps1()
        if path.startswith("/assets/"):
            return self._static(path[len("/assets/"):])

        conn = db()
        try:
            if path == "/v1/leaderboard":
                return self._leaderboard(conn, qs)
            if path == "/v1/agent_leaderboard":
                return self._agent_leaderboard(conn, qs)
            if path == "/v1/teams":
                return self._teams(conn, qs)
            if path == "/v1/cursor":
                return self._cursor(conn, qs)
            if path == "/v1/breakdown":
                return self._breakdown(conn, qs)
            if path == "/v1/trend":
                return self._trend(conn, qs)
            if path == "/v1/ai/usage":
                return self._ai_usage(conn, qs)
            if path == "/v1/meta":
                return self._meta(conn)
            if path == "/v1/governance_metrics":
                return self._governance_metrics(conn, qs)
            if path == "/v1/feishu":
                return self._feishu(conn, qs)
            if path == "/v1/raw":
                return self._raw(conn)
            self._send(200, {
                "service": "dev_collector",
                "endpoints": [
                    "POST /v1/tokscale/report",
                    "GET  /v1/leaderboard            (个人榜, 不含 agent)",
                    "GET  /v1/agent_leaderboard      (agent 榜, 仅 litellm_agent)",
                    "GET  /v1/teams                  (部门/team 榜)",
                    "GET  /v1/breakdown?by=client|client_model|client_provider_model|model  (工具/模型榜)",
                    "GET  /v1/trend?email=...        (月度趋势)",
                    "GET  /v1/ai/usage?user=&days=N  (AI 用: 单人汇总+每日明细; 不传 user 出整榜)",
                    "GET  /v1/governance_metrics     (大厂治理指标可计算项)",
                    "GET  /v1/raw",
                ],
            })
        finally:
            conn.close()

    def _feishu(self, conn, qs):
        """飞书 AI 权益(独立板块,单位=点)。按天聚合:额度盘 + 全员逐人榜 + 部门榜 + 趋势。
        区间同 token 榜:?from=&to= 或 ?days=N(usage_date 上过滤);默认近 30 天(=回填窗口)。
        ?show_departed=1 才纳入离职。"""
        # usage_date 区间(默认近 30 天),统一走 _feishu_range。
        rng, params = _feishu_range(qs)
        billing = {"usd_per_point": FEISHU_USD_PER_POINT, "package_cny": PACKAGE_CNY,
                   "package_points": PACKAGE_POINTS, "cny_per_usd": CNY_PER_USD,
                   "package_usd": (PACKAGE_CNY / CNY_PER_USD) if CNY_PER_USD else 0.0}

        span = conn.execute("SELECT min(usage_date), max(usage_date) FROM feishu_member"
                            " WHERE 1=1%s" % rng, params).fetchone()
        if not span or not span[0]:
            payload = {"period_start": None, "period_end": None, "quota": [],
                       "members": [], "dept": [], "trend": []}
            payload.update(billing)
            return self._send(200, payload)
        ps, pe = span[0], span[1]
        sd = _show_departed(qs)
        dep = "" if sd else " AND email NOT IN (SELECT email FROM departed)"
        quota = [{"feature_key": r[0], "quota": r[1] or 0, "used": r[2] or 0, "remain": r[3] or 0}
                 for r in conn.execute(
                     "SELECT feature_key,quota,used,remain FROM feishu_quota WHERE period_start="
                     "(SELECT max(period_start) FROM feishu_quota) ORDER BY quota DESC").fetchall()]
        members = [{"email": r[0], "name": r[1] or (r[0] or "").split("@")[0], "dept": r[2] or "unknown",
                    "avatar": r[3] or "", "credits": r[4] or 0,
                    "ai_credits": r[5] or 0, "aily_credits": r[6] or 0}
                   for r in conn.execute(
                       "SELECT email, MAX(name), MAX(dept), MAX(avatar), SUM(credits),"
                       " SUM(CASE WHEN feature_key='AI_credits' THEN credits ELSE 0 END),"
                       " SUM(CASE WHEN feature_key='aily_credits' THEN credits ELSE 0 END)"
                       " FROM feishu_member WHERE 1=1%s%s"
                       " GROUP BY email HAVING SUM(credits)>0 ORDER BY SUM(credits) DESC" % (rng, dep),
                       params).fetchall()]
        dept = [{"dept": r[0] or "unknown", "credits": r[1] or 0, "people": r[2] or 0}
                for r in conn.execute(
                    "SELECT dept, SUM(credits), COUNT(DISTINCT email) FROM feishu_member"
                    " WHERE 1=1%s%s GROUP BY dept ORDER BY SUM(credits) DESC" % (rng, dep),
                    params).fetchall()]
        trend = [{"usage_date": r[0], "biz_type": r[1], "credits": r[2] or 0, "user_count": r[3] or 0}
                 for r in conn.execute(
                     "SELECT usage_date,biz_type,credits,user_count FROM feishu_trend"
                     " ORDER BY usage_date").fetchall()]
        payload = {"period_start": ps, "period_end": pe,
                   "quota": quota, "members": members, "dept": dept, "trend": trend}
        payload.update(billing)
        self._send(200, payload)

    def _leaderboard(self, conn, qs):
        """按人聚合(区间 ?days=N 或全部),join people 取中文姓名+头像+完整部门路径。
        同一人当天 Cursor+Claude+Codex 的 token 自动求和(GROUP BY email)。"""
        where, params = _range_clause(qs, "u.")
        sd = _show_departed(qs)
        dep_clause = _departed_filter(sd, "u.")
        departed = _departed_set(conn)
        cost_start, cost_end = _cost_window(qs)
        today = datetime.date.today()
        # 可选 ?client=Claude Code|Codex CLI|... → 只统计该工具(Claude 榜 / Codex 榜复用此端点)
        cli = (qs.get("client") or [None])[0]
        # client 匹配大小写不敏感:历史上 Hermes 有上报端写过小写 'hermes',
        # 精确匹配会把这些行漏出榜单与推断;归一(ingest 已做)之前的存量也要能查到。
        cli_clause = " AND lower(u.client) = ?" if cli else ""
        params2 = list(params) + ([cli.lower()] if cli else [])
        # agent key 用量(source=litellm_agent)不进个人榜 —— 单独走 /v1/agent_leaderboard
        rows = conn.execute("""
            SELECT u.email, MAX(u.dept),
                   SUM(u.input), SUM(u.output), SUM(u.cache_read), SUM(u.cache_write),
                   SUM(u.reasoning), SUM(u.total),
                   SUM(CASE WHEN u.source IN ('litellm','api') THEN u.cost ELSE 0 END),
                   SUM(u.messages),
                   MAX(p.name), MAX(p.avatar),
                   (SELECT rl.via FROM report_log rl WHERE rl.email = u.email
                    ORDER BY rl.reported_at DESC LIMIT 1),
                   MAX(p.dept)
            FROM usage u LEFT JOIN people p ON p.email = u.email
            WHERE %s AND u.source != 'litellm_agent'
              AND u.email NOT LIKE 'litellm-key:%%' AND u.email NOT LIKE 'litellm-user:%%'%s%s
            GROUP BY u.email
            HAVING SUM(u.total) > 0
            ORDER BY SUM(u.total) DESC
        """ % (where, dep_clause, cli_clause), params2).fetchall()
        # 每人按工具(client)的构成:Claude/Codex/Cursor/Gemini/... 占比
        comp = {}
        for cr in conn.execute("""
            SELECT u.email, u.client, SUM(u.total)
            FROM usage u
            WHERE %s AND u.source != 'litellm_agent'
              AND u.email NOT LIKE 'litellm-key:%%' AND u.email NOT LIKE 'litellm-user:%%'%s%s
            GROUP BY u.email, u.client
        """ % (where, dep_clause, cli_clause), params2).fetchall():
            comp.setdefault(cr[0], []).append({"client": cr[1], "tokens": cr[2] or 0})
        result = []
        by_email = {}
        for r in rows:
            # 部门优先用 people.dept(飞连自愈的全路径),裸的 MAX(u.dept) 只兜底 ——
            # 否则 LiteLLM 团队别名(裸"技术平台部")会被 MAX 误选盖掉飞连全路径(中文排在 'K' 之后)。
            row = {
                "email": r[0], "dept": _to_keep(r[13]) or _normalize_dept_path(r[1]),
                "input": r[2] or 0, "output": r[3] or 0,
                "cache_read": r[4] or 0, "cache_write": r[5] or 0,
                "reasoning": r[6] or 0, "tokens": r[7] or 0,
                # 个人榜 cost 口径改为公司实付：这里只先放网关真实花费，订阅费稍后按窗口叠加。
                "cost": round(r[8] or 0, 4), "messages": r[9] or 0,
                "name": r[10] or (r[0] or "").split("@")[0],
                "avatar": r[11] or "",
                "via": r[12] or "",   # 最近一次订阅制上报来源:manual=手工补报(看板打角标)
                "departed": r[0] in departed,
                "composition": list(comp.get(r[0], [])),   # pct 等飞书并入后统一算
                "subs": [],
            }
            result.append(row)
            by_email[r[0]] = row

        # 工具榜只回传对应工具的单个订阅,不把订阅费并进 cost。
        subs_by_email = load_subscriptions(conn)
        sub_tool = _CLIENT_TO_SUB_TOOL.get(cli)
        if cli:
            # lifetime(无窗口)时与个人榜同口径:窗口取「该人最早用量日 → 今天」,
            # 都没有时才退化为今天;无界席位在完全无用量时按 1.0 个月费计。
            cli_window_start = {}
            if cost_start is None:
                for mr in conn.execute("""
                    SELECT u.email, MIN(u.period)
                    FROM usage u
                    WHERE u.period_type='day' AND u.source != 'litellm_agent'%s
                    GROUP BY u.email
                """ % cli_clause, ([cli.lower()] if cli else [])).fetchall():
                    if mr[0] and mr[1]:
                        cli_window_start[mr[0]] = mr[1]
            for row in result:
                if cost_start is not None and cost_end is not None:
                    win_s, win_e = cost_start, cost_end
                else:
                    win_s, win_e = cli_window_start.get(row["email"]) or today, today
                row["subs"] = _single_tool_subs(subs_by_email, row["email"], sub_tool, win_s, win_e)
                # 工具榜价格:该工具订阅费按席位区间摊销并入本榜 cost(订阅 token 无网关
                # 实销,此前恒为 $0 —— sunke 2026-06-13"codex/claude 榜没有价格")。
                if sub_tool in ("claude", "codex"):   # SPEC: Cursor 榜(/v1/cursor)不动
                    fee_total = 0.0
                    for seat in subs_by_email.get(row["email"], []):
                        if (seat.get("tool") or "").lower() != sub_tool:
                            continue
                        if cost_start is None and cli_window_start.get(row["email"]) is None and                                 not seat.get("start") and not seat.get("end"):
                            frac = 1.0
                        else:
                            frac = _interval_fraction(win_s, win_e, seat.get("start"), seat.get("end"))
                        fee_total += float(seat.get("fee") or 0) * frac
                    if fee_total:
                        row["cost"] = round(float(row["cost"] or 0) + fee_total, 4)

        # Hermes 榜价格(sunke 2026-06-13):Hermes 上报自带 cost 的行用原值;cost=0 的行
        # 用上游 LiteLLM 同模型实际单价(sum(cost)/sum(total))推断估价 —— 模型名先精确、
        # 再取「最后一段去厂商前缀」匹配(tencent/glm-5.1 → glm-5.1);LiteLLM 没有的模型
        # 不标价。推断价只进本榜展示(cost_est 标记,前端显 ≈$),不进个人榜公司实付。
        if cli and cli.lower() == "hermes":
            rate_exact, rate_stripped = {}, {}
            for m, co, t in conn.execute(
                    "SELECT model, SUM(cost), SUM(total) FROM usage"
                    " WHERE source='litellm' AND period_type='day' AND total>0"
                    " GROUP BY model").fetchall():
                if t and co:
                    rate_exact[m or ""] = co / t
                    rate_stripped.setdefault((m or "").split("/")[-1].lower(), co / t)
            rep_by_email, est_by_email = {}, {}
            # rep 只累计「非网关 source 自带的 cost」—— api/litellm 的成本主查询已并入
            # row.cost,再加就是重复计费;est 只对 cost=0 的那部分 token 推断,同人同
            # 模型「带价行+零价行」混合时零价部分照样推断(按行内 CASE 拆分,不看组总)。
            for em, m, rep_cost, zero_tok in conn.execute("""
                SELECT u.email, u.model,
                       SUM(CASE WHEN u.cost > 0 AND u.source NOT IN ('litellm','api')
                                THEN u.cost ELSE 0 END),
                       SUM(CASE WHEN u.cost <= 0 THEN u.total ELSE 0 END)
                FROM usage u
                WHERE %s AND u.source != 'litellm_agent'%s%s
                GROUP BY u.email, u.model
            """ % (where, dep_clause, cli_clause), params2).fetchall():
                if rep_cost and rep_cost > 0:
                    rep_by_email[em] = rep_by_email.get(em, 0.0) + rep_cost
                r = rate_exact.get(m or "") or rate_stripped.get((m or "").split("/")[-1].lower())
                if r and zero_tok:
                    est_by_email[em] = est_by_email.get(em, 0.0) + zero_tok * r
            for row in result:
                rep = rep_by_email.get(row["email"], 0.0)
                est = est_by_email.get(row["email"], 0.0)
                if rep or est:
                    row["cost"] = round(float(row["cost"] or 0) + rep + est, 4)
                if est:
                    row["cost_est"] = True

        # 飞书 AI 权益并入个人榜:1 点 = 1 token,计入总量参与排序(孙可 2026-06-12)。
        # 只在主个人榜并入;带 ?client 的工具榜(Claude/Codex/Cursor/LiteLLM/Hermes)不并。
        # 纯飞书用户(无 token 用量)也建行进榜,排序靠后无妨(孙可:不用显出来)。
        if not cli:
            frng, fparams = _feishu_range(qs)
            fdep = "" if sd else " AND email NOT IN (SELECT email FROM departed)"
            for fr in conn.execute(
                "SELECT email, MAX(name), MAX(dept), MAX(avatar), SUM(credits)"
                " FROM feishu_member WHERE 1=1%s%s"
                " GROUP BY email HAVING SUM(credits)>0" % (frng, fdep), fparams).fetchall():
                fem, fname, fdept, favatar, credits = fr[0], fr[1], fr[2], fr[3], fr[4] or 0
                if credits <= 0:
                    continue
                row = by_email.get(fem)
                if row is None:
                    pr = conn.execute("SELECT dept,name,avatar FROM people WHERE email=?",
                                      (fem,)).fetchone()
                    dept = (_to_keep(pr[0]) if pr and pr[0] else None) \
                        or _to_keep(fdept) or _normalize_dept_path(fdept) or "unknown"
                    row = {"email": fem, "dept": dept,
                           "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                           "reasoning": 0, "tokens": 0, "cost": 0, "messages": 0,
                           "name": (pr[1] if pr and pr[1] else None) or fname or fem.split("@")[0],
                           "avatar": (pr[2] if pr and pr[2] else None) or favatar or "",
                           "via": "", "departed": fem in departed, "composition": [],
                           "subs": []}
                    result.append(row)
                    by_email[fem] = row
                row["tokens"] = (row["tokens"] or 0) + credits
                row["feishu_credits"] = credits   # 单列飞书点数,前端可标注「含飞书 X 点」
                fc = round(credits * FEISHU_USD_PER_POINT, 4)
                row["feishu_cost"] = fc
                row["cost"] = round(float(row["cost"] or 0) + credits * FEISHU_USD_PER_POINT, 4)
                row["composition"].append({"client": "飞书AI权益", "tokens": credits})

            usage_window_start = {}
            if cost_start is None:
                for mr in conn.execute("""
                    SELECT u.email, MIN(u.period)
                    FROM usage u
                    WHERE u.period_type='day' AND u.source != 'litellm_agent'%s
                    GROUP BY u.email
                """ % cli_clause, ([cli.lower()] if cli else [])).fetchall():
                    if mr[0] and mr[1]:
                        usage_window_start[mr[0]] = mr[1]

            def _fee_window(email):
                if cost_start is not None and cost_end is not None:
                    return cost_start, cost_end
                return usage_window_start.get(email) or today, today

            for row in result:
                win_s, win_e = _fee_window(row["email"])
                kept = []
                fee_total = 0.0
                for sub in subs_by_email.get(row["email"], []):
                    if cost_start is None and usage_window_start.get(row["email"]) is None and \
                            not sub.get("start") and not sub.get("end"):
                        # lifetime/all 且此人无 usage 明细时，保留原「整月订阅=1.0」语义。
                        frac = 1.0
                    else:
                        frac = _interval_fraction(win_s, win_e, sub.get("start"), sub.get("end"))
                    if frac <= 0:
                        continue
                    kept.append(sub)
                    fee_total += float(sub.get("fee") or 0) * frac
                row["subs"] = _group_subs(kept)
                if not kept:
                    continue
                row["cost"] = round(float(row["cost"] or 0) + fee_total, 4)

        # 飞书并入后总量可能变 → 统一算 composition 占比 + 重排
        for row in result:
            tot = row["tokens"] or 0
            for x in row["composition"]:
                x["pct"] = round(x["tokens"] / tot * 100, 1) if tot else 0
            row["composition"].sort(key=lambda x: x["tokens"], reverse=True)
        result.sort(key=lambda x: x["tokens"] or 0, reverse=True)
        self._send(200, {"leaderboard": result})

    def _agent_leaderboard(self, conn, qs):
        """Agent 专属榜:只看 source='litellm_agent', 按 key_alias(email='agent:<alias>')聚合.
        与个人榜完全隔离 —— agent 永不进个人榜, 个人 key 也永不进这里。"""
        # people 行(agent:<alias>): name=alias, dept=归属人中文名, avatar=归属人头像
        where, params = _range_clause(qs, "u.")
        rows = conn.execute("""
            SELECT u.email, MAX(p.dept),
                   SUM(u.input), SUM(u.output), SUM(u.cache_read), SUM(u.cache_write),
                   SUM(u.reasoning), SUM(u.total), SUM(u.cost), SUM(u.messages),
                   MAX(p.name), MAX(p.avatar)
            FROM usage u LEFT JOIN people p ON p.email = u.email
            WHERE %s AND u.source = 'litellm_agent'
            GROUP BY u.email
            ORDER BY SUM(u.total) DESC
        """ % where, params).fetchall()
        result = []
        for r in rows:
            alias = r[10] or (r[0] or "").split(":", 1)[-1]
            result.append({
                "agent": alias, "email": r[0],
                "owner": r[1] or "",            # 归属人(中文名)
                "avatar": r[11] or "",          # 归属人头像
                "input": r[2] or 0, "output": r[3] or 0,
                "cache_read": r[4] or 0, "cache_write": r[5] or 0,
                "reasoning": r[6] or 0, "tokens": r[7] or 0,
                "cost": round(r[8] or 0, 4), "messages": r[9] or 0,
                "name": alias,
            })
        self._send(200, {"agent_leaderboard": result})

    def _cursor(self, conn, qs):
        """Cursor 维度榜:按 token 排(与个人/工具/模型榜口径统一),带 token 明细 +
        花费($)/请求数 + 中文姓名/头像/部门。token 来自 Cursor Admin API 的
        filtered-usage-events.tokenUsage(真 token,见 cursor_sync.py)。支持全局区间。"""
        where, params = _range_clause(qs, "u.")
        sd = _show_departed(qs)
        dep_clause = _departed_filter(sd, "u.")
        departed = _departed_set(conn)
        rows = conn.execute("""
            SELECT u.email, MAX(u.dept),
                   SUM(u.input), SUM(u.output), SUM(u.cache_read), SUM(u.cache_write),
                   SUM(u.reasoning), SUM(u.total), SUM(u.cost), SUM(u.messages),
                   MAX(p.name), MAX(p.avatar), MAX(p.dept)
            FROM usage u LEFT JOIN people p ON p.email = u.email
            WHERE %s AND u.source='cursor'%s
            GROUP BY u.email
            ORDER BY SUM(u.total) DESC
        """ % (where, dep_clause), params).fetchall()
        result = []
        # Cursor 榜只挂 cursor 订阅徽标,不带其他工具。
        subs_by_email = load_subscriptions(conn)
        cost_start, cost_end = _cost_window(qs)
        today = datetime.date.today()
        win_s = cost_start if cost_start is not None else today
        win_e = cost_end if cost_end is not None else today
        for r in rows:
            result.append({
                "email": r[0], "dept": _to_keep(r[12]) or _normalize_dept_path(r[1]),  # 优先 people.dept(飞连全路径)
                "input": r[2] or 0, "output": r[3] or 0,
                "cache_read": r[4] or 0, "cache_write": r[5] or 0,
                "reasoning": r[6] or 0, "tokens": r[7] or 0,
                "cost": round(r[8] or 0, 2), "requests": r[9] or 0,
                "name": r[10] or (r[0] or "").split("@")[0], "avatar": r[11] or "",
                "departed": r[0] in departed,
                "subs": _single_tool_subs(subs_by_email, r[0], "cursor", win_s, win_e),
            })
        self._send(200, {"cursor": result})

    def _teams(self, conn, qs):
        """按部门(team)聚合(区间或全部)。dept 完整路径,含使用人数(people)+部门总人数
        (headcount,来自飞连)+活跃率(active_rate=people/headcount*100)。跨工具求和。
        默认剔除离职用户(token 与人数都不计);?show_departed=1 时纳入。"""
        where, params = _range_clause(qs)
        sd = _show_departed(qs)
        dep_clause = _departed_filter(sd, "")
        # 取 email 级明细。注意 usage.dept 异构：订阅制/cursor 是完整路径
        # ('Keep/技术平台部/.../IT 组')，LiteLLM 却是裸团队别名('技术平台部')。
        # 若直接按 usage.dept roll-up，裸别名会裂成不挂在 Keep 树下的孤立顶级节点。
        rows = conn.execute("""
            SELECT email, dept, SUM(total), SUM(cost), SUM(messages)
            FROM usage
            WHERE %s AND source != 'litellm_agent'%s
            GROUP BY email, dept
        """ % (where, dep_clause), params).fetchall()

        # 用 people.dept(飞连规范全路径)把每个人归一到唯一的真实组织部门，
        # 再把此人所有来源的用量收进该部门 → 单一 Keep 树，杜绝裸别名裂树。
        pdept = dict(conn.execute("SELECT email, dept FROM people").fetchall())

        per = {}  # email -> {tok, cost, msg, depts:[...]}
        for email, dept, tok, cost, msg in rows:
            p = per.get(email)
            if p is None:
                p = {"tok": 0, "cost": 0.0, "msg": 0, "depts": []}
                per[email] = p
            p["tok"] += tok or 0
            p["cost"] += cost or 0
            p["msg"] += msg or 0
            if dept:
                p["depts"].append(dept)

        def _canon_dept(email, depts):
            """每人规范部门：people.dept 优先 → usage 里最具体的可归一 Keep 路径 →
            都归不到则 'Keep/未归类'。统一过 _to_keep：外包折回真实部门、裸供应商(SP码)收口外部合作商，
            与 headcount 同口径。裸非 SP 组名/unknown 归不到 Keep → 未归类。"""
            cand = _to_keep(pdept.get(email))
            if cand:
                return cand
            keeps = [c for c in (_to_keep(x) for x in depts) if c]
            if keeps:
                return max(keeps, key=len)
            return "Keep/未归类"

        # 部门总人数(飞连,缓存,懒加载,graceful)：叶子级 headcount 同样 roll-up 到每级父部门。
        # 注意:dept_headcount.json 是 6h 文件缓存,旧缓存里仍是未归并的 'Keep/合作商/V/...' 路径,
        # 命中/兜底时不会重算 → 必须在消费点再归一化一次(幂等),否则归并后的真实叶子拿不到 headcount。
        headcount_map = _dept_headcount_map()
        node_hc = {}
        for path, cnt in headcount_map.items():
            for anc in _ancestors(_normalize_dept_path(path)):
                node_hc[anc] = node_hc.get(anc, 0) + (cnt or 0)

        def _node(path):
            n = nodes.get(path)
            if n is None:
                # token_users/aily_users 分开:人均按各自口径,活跃渗透取并集
                n = {"tokens": 0, "cost": 0.0, "messages": 0, "credits": 0.0,
                     "token_users": set(), "aily_users": set()}
                nodes[path] = n
            return n

        nodes = {}  # path -> {tokens, cost, messages, credits, token_users, aily_users}
        for email, p in per.items():
            cd = _canon_dept(email, p["depts"])
            if cd == "Keep/未归类":
                continue   # 解析不到真实部门(离职/飞连外)→ 跳过,不污染部门榜(孙可 2026-06-11)
            for anc in _ancestors(cd):
                n = _node(anc)
                n["tokens"] += p["tok"]
                n["cost"] += p["cost"]
                n["messages"] += p["msg"]
                n["token_users"].add(email)

        # aily(飞书 AI 权益)并入部门榜:按天聚合,跟随所选区间(与 token 同窗口,默认近30天),
        # 不再取「最新月快照」死数据(孙可 2026-06-12:月累计污染部门榜)。单位「点」credits,
        # 不与 token 加总;aily 的人并进活跃集 → 活跃渗透取并集。
        frng, fparams = _feishu_range(qs)
        fdep = "" if sd else " AND email NOT IN (SELECT email FROM departed)"
        aily_rows = conn.execute(
            "SELECT email, MAX(dept), SUM(credits) FROM feishu_member"
            " WHERE 1=1%s%s GROUP BY email HAVING SUM(credits)>0" % (frng, fdep),
            fparams).fetchall()
        for email, fdept, credits in aily_rows:
            # people.dept 优先,否则用 feishu_member.dept;统一过 _to_keep —— 裸供应商(SP码)
            # 也收口到外部合作商,不再因「不以 Keep 开头」误落未归类(codex 评审发现)。
            cd = _to_keep(pdept.get(email)) or _to_keep(fdept)
            if not cd:
                continue   # 飞连查不到真实部门(离职/飞连外纯飞书用户)→ 跳过,不进未归类(孙可 2026-06-11)
            for anc in _ancestors(cd):
                n = _node(anc)
                n["credits"] += credits or 0
                n["aily_users"].add(email)

        result = []
        for path, n in nodes.items():
            token_people = len(n["token_users"])
            aily_people = len(n["aily_users"])
            people = len(n["token_users"] | n["aily_users"])   # 活跃 = token ∪ aily(去重)
            hc = node_hc.get(path)
            if hc and hc > 0:
                active_rate = round(people / float(hc) * 100, 1)
            else:
                hc = None
                active_rate = None
            result.append({
                "dept": path,
                "depth": path.count("/"),     # 'Keep'=0, 'Keep/技术平台部'=1 ... 供前端建树/缩进
                "people": people,             # 活跃人数(token∪aily),供活跃渗透
                "token_people": token_people,
                "aily_people": aily_people,
                "headcount": hc,
                "active_rate": active_rate,
                "tokens": n["tokens"], "cost": round(n["cost"], 4),
                "messages": n["messages"],
                "credits": round(n["credits"], 2),  # aily 总点数(单位「点」,不与 token 加总)
                "per_capita_tokens": round(n["tokens"] / token_people) if token_people else 0,
                "per_capita_credits": round(n["credits"] / aily_people, 1) if aily_people else 0,
            })
        result.sort(key=lambda x: -x["tokens"])
        self._send(200, {"teams": result})

    def _breakdown(self, conn, qs):
        """四种维度聚合 lifetime 快照。
        by=client                  → 按 client 聚合
        by=client_model            → 按 client + model 聚合
        by=client_provider_model   → 按 client + provider + model 聚合（默认）
        """
        by = (qs.get("by") or ["client_provider_model"])[0]
        if by == "client":
            group_cols = "client"
            select_extra = "client, '' AS provider, '' AS model"
        elif by == "model":
            group_cols = "model"
            select_extra = "'' AS client, '' AS provider, model"
        elif by == "client_model":
            group_cols = "client, model"
            select_extra = "client, '' AS provider, model"
        else:
            group_cols = "client, provider, model"
            select_extra = "client, provider, model"

        where, params = _range_clause(qs)
        sql = (
            "SELECT {extra}, "
            "SUM(input), SUM(output), SUM(cache_read), SUM(cache_write), "
            "SUM(reasoning), SUM(total), SUM(cost), SUM(messages) "
            "FROM usage WHERE {where} AND source != 'litellm_agent' "
            "GROUP BY {grp} ORDER BY SUM(total) DESC"
        ).format(extra=select_extra, where=where, grp=group_cols)

        rows = conn.execute(sql, params).fetchall()
        result = []
        for r in rows:
            result.append({
                "client": r[0], "provider": r[1], "model": r[2],
                "input": r[3] or 0, "output": r[4] or 0,
                "cache_read": r[5] or 0, "cache_write": r[6] or 0,
                "reasoning": r[7] or 0, "tokens": r[8] or 0,
                "cost": round(r[9] or 0, 4), "messages": r[10] or 0,
            })
        self._send(200, {"by": by, "breakdown": result})

    def _trend(self, conn, qs):
        """月度时间序列（period_type=month）。可选 ?email=xxx 过滤。"""
        email_filter = (qs.get("email") or [None])[0]
        if email_filter:
            rows = conn.execute("""
                SELECT period, SUM(input), SUM(output), SUM(cache_read),
                       SUM(cache_write), SUM(reasoning), SUM(total), SUM(cost), SUM(messages)
                FROM usage
                WHERE period_type='month' AND email=?
                GROUP BY period ORDER BY period
            """, (email_filter,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT period, SUM(input), SUM(output), SUM(cache_read),
                       SUM(cache_write), SUM(reasoning), SUM(total), SUM(cost), SUM(messages)
                FROM usage
                WHERE period_type='month'
                GROUP BY period ORDER BY period
            """).fetchall()
        result = []
        for r in rows:
            result.append({
                "month": r[0],
                "input": r[1] or 0, "output": r[2] or 0,
                "cache_read": r[3] or 0, "cache_write": r[4] or 0,
                "reasoning": r[5] or 0, "tokens": r[6] or 0,
                "cost": round(r[7] or 0, 4), "messages": r[8] or 0,
            })
        self._send(200, {"email": email_filter, "trend": result})

    def _ai_usage(self, conn, qs):
        """AI 用的无鉴权用量接口(喂 Hermes skill)。

        - ?user=<邮箱或登录名>&days=N → 该人窗口内 token 汇总 + 每日明细。
          登录名(无 @)自动补公司域名 AI_EMAIL_DOMAIN; 邮箱大小写不敏感。
          查不到人不报 404, 返回 total_tokens=0/daily=[]。
        - 不传 user → 窗口内按人 SUM 的 top-N 个人榜(默认排除离职; ?show_departed=1 纳入)。
        窗口语义同其它榜: ?from=&to= 优先, 否则 ?days=N, 默认近 30 天(只看 day 桶)。
        每条响应都带数据时间戳: latest_usage_date(数据覆盖到哪天) + generated_at。
        """
        frm = (qs.get("from") or [None])[0]
        to = (qs.get("to") or [None])[0]
        days = None
        if not (frm or to):
            raw = (qs.get("days") or [None])[0]
            try:
                days = int(raw) if raw not in (None, "", "all") else 30
            except (TypeError, ValueError):
                days = 30
            if days <= 0:
                days = 30
            to = datetime.date.today().isoformat()
            frm = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()

        def clause(pfx=""):
            cs = ["%speriod_type='day'" % pfx]
            if frm:
                cs.append("%speriod >= ?" % pfx)
            if to:
                cs.append("%speriod <= ?" % pfx)
            return " AND ".join(cs)

        params = []
        if frm:
            params.append(frm)
        if to:
            params.append(to)

        latest = conn.execute(
            "SELECT max(period) FROM usage WHERE period_type='day'").fetchone()[0]
        now_iso = datetime.datetime.now().isoformat(timespec="seconds")
        window = {"days": days, "from": frm, "to": to}

        sd = _show_departed(qs)
        departed_lower = {(e or "").lower() for e in _departed_set(conn)}
        # 口径完全对齐前端个人榜 /v1/leaderboard(sunke 2026-06-16):
        #  - token = SUM(total) + 飞书权益点(1点=1token)
        #  - cost  = 公司实付 = 网关实销 SUM(CASE source IN('litellm','api') THEN cost) + 飞书点成本
        #    订阅制客户端(Claude Code/Codex)按 token 的牌价 cost 绝不计入(否则千倍虚高)。
        # agent key 用量(litellm_agent)与合成身份(litellm-key/-user)不进个人统计。
        PERSON_FILTER = (" AND source != 'litellm_agent'"
                         " AND email NOT LIKE 'litellm-key:%%'"
                         " AND email NOT LIKE 'litellm-user:%%'")

        def cost_case(pfx=""):
            return ("SUM(CASE WHEN %ssource IN ('litellm','api') THEN %scost ELSE 0 END)"
                    % (pfx, pfx))

        # 飞书权益点窗口(usage_date,语义同 ?days/from/to)。
        frng, fparams = _feishu_range(qs)

        user = (qs.get("user") or [None])[0]
        if user and user.strip():
            email = user.strip().lower()
            if "@" not in email:
                email = "%s@%s" % (email, AI_EMAIL_DOMAIN)
            is_departed = email in departed_lower
            if is_departed and not sd:
                # 默认排除离职: 显式查到离职者返回 0, 带 departed 标记(非静默 0); ?show_departed=1 才出数。
                daily, total_tokens, cost_usd = [], 0, 0
            else:
                rows = conn.execute(
                    "SELECT period, SUM(total), %s FROM usage "
                    "WHERE %s%s AND lower(email)=? GROUP BY period"
                    % (cost_case(), clause(), PERSON_FILTER),
                    params + [email]).fetchall()
                day_map = {r[0]: {"date": r[0], "total_tokens": r[1] or 0,
                                  "cost_usd": float(r[2] or 0)} for r in rows}
                # 飞书点按天并入(1点=1token; 点成本 credits×USD_PER_POINT 并入 cost),
                # 折到对应天使 sum(daily)==total。
                for ud, cr in conn.execute(
                        "SELECT usage_date, SUM(credits) FROM feishu_member "
                        "WHERE 1=1%s AND lower(email)=? GROUP BY usage_date" % frng,
                        fparams + [email]).fetchall():
                    cr = cr or 0
                    if not cr:
                        continue
                    d = day_map.setdefault(ud, {"date": ud, "total_tokens": 0, "cost_usd": 0.0})
                    d["total_tokens"] += cr
                    d["cost_usd"] += cr * FEISHU_USD_PER_POINT
                daily = [{"date": d["date"], "total_tokens": d["total_tokens"],
                          "cost_usd": round(d["cost_usd"], 4)}
                         for d in sorted(day_map.values(), key=lambda x: x["date"])]
                total_tokens = sum(d["total_tokens"] for d in daily)
                cost_usd = round(sum(d["cost_usd"] for d in daily), 4)
            prof = conn.execute(
                "SELECT MAX(name), MAX(dept) FROM people WHERE lower(email)=?",
                (email,)).fetchone()
            name = (prof[0] if prof else None) or None
            dept = (prof[1] if prof else None) or None
            if not dept:
                d2 = conn.execute(
                    "SELECT MAX(dept) FROM usage WHERE lower(email)=?",
                    (email,)).fetchone()
                dept = (d2[0] if d2 and d2[0] else None) or None
            return self._send(200, {
                "user": email, "name": name, "dept": dept,
                "departed": is_departed,
                "window": window,
                "total_tokens": total_tokens, "cost_usd": cost_usd,
                "daily": daily,
                "latest_usage_date": latest, "generated_at": now_iso,
            })

        # 不传 user → 整张个人榜(窗口内按人聚合), 默认排除离职 + agent/合成身份。
        dep = _departed_filter(sd, "u.")
        fdep = "" if sd else " AND email NOT IN (SELECT email FROM departed)"
        try:
            limit = int((qs.get("limit") or ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        if limit <= 0:
            limit = 50
        rows = conn.execute(
            "SELECT u.email, MAX(p.name), COALESCE(MAX(p.dept), MAX(u.dept)), "
            "SUM(u.total), %s "
            "FROM usage u LEFT JOIN people p ON p.email = u.email "
            "WHERE %s AND u.source != 'litellm_agent' "
            "AND u.email NOT LIKE 'litellm-key:%%' AND u.email NOT LIKE 'litellm-user:%%'%s "
            "GROUP BY u.email HAVING SUM(u.total) > 0 "
            "ORDER BY SUM(u.total) DESC LIMIT ?" % (cost_case("u."), clause("u."), dep),
            params + [limit]).fetchall()
        # 飞书点并入已在榜的人(1点=1token + 点成本); 纯飞书用户(无 agent token)不在 top-N,
        # 单人查询能查到其飞书点, 此处不另建行(与单人路径口径一致, 量级可忽略)。
        fcred = {}
        for fe, cr in conn.execute(
                "SELECT lower(email), SUM(credits) FROM feishu_member "
                "WHERE 1=1%s%s GROUP BY lower(email)" % (frng, fdep), fparams).fetchall():
            if cr:
                fcred[fe] = cr
        ranking = []
        for r in rows:
            cr = fcred.get((r[0] or "").lower(), 0)
            ranking.append({
                "user": r[0], "name": r[1] or None, "dept": r[2] or None,
                "total_tokens": (r[3] or 0) + cr,
                "cost_usd": round(float(r[4] or 0) + cr * FEISHU_USD_PER_POINT, 4)})
        self._send(200, {
            "window": window, "count": len(ranking), "ranking": ranking,
            "latest_usage_date": latest, "generated_at": now_iso,
        })

    def _governance_metrics(self, conn, qs=None):
        """当前 SQLite 能直接计算的治理指标。

        只使用聚合 usage/report_log 数据；不读取 prompt、代码正文或任何凭证。
        """
        def _num(v):
            return int(v or 0)

        def _money(v):
            return float(v or 0)

        def _fmt_int(v):
            return "{:,}".format(_num(v))

        def _fmt_money(v, digits=0):
            return "${:,.{digits}f}".format(_money(v), digits=digits)

        def _pct(part, total):
            total = float(total or 0)
            if not total:
                return "0.0%"
            return "{:.1f}%".format(float(part or 0) / total * 100)

        def _compact(v):
            n = float(v or 0)
            if abs(n) >= 100000000:
                return "{:.1f} 亿".format(n / 100000000.0)
            if abs(n) >= 10000:
                return "{:.1f} 万".format(n / 10000.0)
            return _fmt_int(n)

        def _cost_per_million(cost, tokens):
            tokens = float(tokens or 0)
            if not tokens:
                return 0.0
            return float(cost or 0) / (tokens / 1000000.0)

        def _top_names(rows, key):
            names = [r.get(key) or "unknown" for r in rows[:3]]
            return " / ".join(names) if names else "暂无"

        lifetime_row = conn.execute("""
            SELECT COUNT(DISTINCT email),
                   COUNT(DISTINCT CASE WHEN dept != '' THEN dept END),
                   COUNT(DISTINCT client),
                   COALESCE(SUM(total),0), COALESCE(SUM(cost),0),
                   COALESCE(SUM(messages),0), COALESCE(SUM(cache_read),0),
                   COALESCE(SUM(cache_write),0), COALESCE(SUM(input),0),
                   COALESCE(SUM(output),0)
            FROM usage WHERE period_type='lifetime'
        """).fetchone()
        day_row = conn.execute("""
            SELECT MIN(period), MAX(period), COUNT(DISTINCT period),
                   COUNT(DISTINCT email), COUNT(*),
                   COALESCE(SUM(total),0), COALESCE(SUM(cost),0)
            FROM usage WHERE period_type='day'
        """).fetchone()
        report_row = conn.execute("""
            SELECT COUNT(*), COUNT(DISTINCT serial), COUNT(DISTINCT email),
                   COALESCE(SUM(CASE WHEN via='manual' THEN 1 ELSE 0 END),0),
                   MAX(reported_at)
            FROM report_log
        """).fetchone()
        try:
            subs_unresolved = conn.execute(
                "SELECT COUNT(*) FROM subscriptions_unresolved"
            ).fetchone()[0]
        except Exception:
            subs_unresolved = 0
        subs_by_email = load_subscriptions(conn)
        # 闲置判定与榜单同窗口（?days/?from-to；缺省 lifetime）。
        # - usage 过滤走 _range_clause(qs)：窗口内有任何用量的人不算闲置；
        # - 席位过滤：席位区间须与窗口重叠 —— 窗口前已删除的席位是「已退订」而非闲置，
        #   不进闲置计数/月费；lifetime 模式取「今天仍在订」。
        qs = qs or {}
        idle_where, idle_params = _range_clause(qs)
        idle_usage_rows = conn.execute(
            "SELECT DISTINCT email FROM usage "
            "WHERE %s AND source != 'litellm_agent' AND COALESCE(email, '') != ''" % idle_where,
            idle_params,
        ).fetchall()
        usage_emails = {r[0] for r in idle_usage_rows if r and r[0]}
        # 飞书 AI 权益点数也算「用量」：纯飞书用户已进个人榜(计订阅费),不能同时再算闲置。
        try:
            frng, fparams = _feishu_range(qs)
            for fr in conn.execute(
                    "SELECT DISTINCT email FROM feishu_member WHERE credits>0%s" % frng,
                    fparams).fetchall():
                if fr and fr[0]:
                    usage_emails.add(fr[0])
        except Exception:
            pass  # feishu_member 表不存在(未启用飞书采集)时跳过
        today_d = datetime.date.today()
        idle_win_s, idle_win_e = _window_dates(qs) or (today_d, today_d)
        idle_fee_by = {}
        idle_emails = set()
        for email, subs in subs_by_email.items():
            if email in usage_emails:
                continue
            for sub in subs:
                if not _interval_overlaps(idle_win_s, idle_win_e,
                                          sub.get("start"), sub.get("end")):
                    continue
                idle_emails.add(email)
                key = (email, sub.get("tool") or "")
                idle_fee_by[key] = idle_fee_by.get(key, 0.0) + float(sub.get("fee") or 0)
        idle_people = [{"email": k[0], "tool": k[1], "fee": round(v, 4)}
                       for k, v in sorted(idle_fee_by.items())]
        # count 按空置订阅人头算；同一人多个 tool 只计 1 人，但 monthly_fee_usd 累加全部席位。
        idle_subscriptions = {
            "count": len(idle_emails),
            "monthly_fee_usd": round(sum(p["fee"] for p in idle_people), 4),
            "people": idle_people,
        }

        max_date = (day_row[1] if day_row else "") or ""
        if max_date:
            last7_row = conn.execute("""
                SELECT COUNT(DISTINCT email), COALESCE(SUM(total),0), COALESCE(SUM(cost),0)
                FROM usage
                WHERE period_type='day' AND period >= date(?, '-6 day')
            """, (max_date,)).fetchone()
        else:
            last7_row = (0, 0, 0)

        source_rows = conn.execute("""
            SELECT source, COUNT(DISTINCT email), COALESCE(SUM(total),0), COALESCE(SUM(cost),0)
            FROM usage
            WHERE period_type='lifetime'
            GROUP BY source
            ORDER BY COALESCE(SUM(total),0) DESC
        """).fetchall()
        client_rows = conn.execute("""
            SELECT client, COUNT(DISTINCT email), COALESCE(SUM(total),0), COALESCE(SUM(cost),0)
            FROM usage
            WHERE period_type='lifetime'
            GROUP BY client
            ORDER BY COALESCE(SUM(total),0) DESC
        """).fetchall()

        sources = [
            {"source": r[0] or "unknown", "users": _num(r[1]),
             "tokens": _num(r[2]), "cost": round(_money(r[3]), 4)}
            for r in source_rows
        ]
        clients = [
            {"client": r[0] or "unknown", "users": _num(r[1]),
             "tokens": _num(r[2]), "cost": round(_money(r[3]), 4)}
            for r in client_rows
        ]
        source_map = {s["source"]: s for s in sources}

        lifetime = {
            "users": _num(lifetime_row[0] if lifetime_row else 0),
            "depts": _num(lifetime_row[1] if lifetime_row else 0),
            "clients": _num(lifetime_row[2] if lifetime_row else 0),
            "tokens": _num(lifetime_row[3] if lifetime_row else 0),
            "cost": round(_money(lifetime_row[4] if lifetime_row else 0), 4),
            "messages": _num(lifetime_row[5] if lifetime_row else 0),
            "cache_read": _num(lifetime_row[6] if lifetime_row else 0),
            "cache_write": _num(lifetime_row[7] if lifetime_row else 0),
            "input": _num(lifetime_row[8] if lifetime_row else 0),
            "output": _num(lifetime_row[9] if lifetime_row else 0),
        }
        day = {
            "min_date": (day_row[0] if day_row else "") or "",
            "max_date": max_date,
            "days": _num(day_row[2] if day_row else 0),
            "active_users": _num(day_row[3] if day_row else 0),
            "rows": _num(day_row[4] if day_row else 0),
            "tokens": _num(day_row[5] if day_row else 0),
            "cost": round(_money(day_row[6] if day_row else 0), 4),
        }
        report_log = {
            "reports": _num(report_row[0] if report_row else 0),
            "devices": _num(report_row[1] if report_row else 0),
            "reporters": _num(report_row[2] if report_row else 0),
            "manual_reports": _num(report_row[3] if report_row else 0),
            "last_report": (report_row[4] if report_row else "") or "",
        }
        last7 = {
            "users": _num(last7_row[0] if last7_row else 0),
            "tokens": _num(last7_row[1] if last7_row else 0),
            "cost": round(_money(last7_row[2] if last7_row else 0), 4),
        }

        cpm = _cost_per_million(lifetime["cost"], lifetime["tokens"])
        cursor = source_map.get("cursor") or {"users": 0, "tokens": 0}
        agent = source_map.get("litellm_agent") or {"users": 0, "tokens": 0}
        freshness = ("数据至 " + day["max_date"]) if day["max_date"] else "暂无日粒度数据"

        metrics = [
            {
                "id": "cost_efficiency",
                "family": "Meta Scuba/Hive · FinOps",
                "label": "成本效率",
                "value": "{} / 1M tok".format(_fmt_money(cpm, 2 if cpm < 10 else 0)),
                "status": "computed",
                "availability": "computed",
                "benchmark": "Meta 热冷分层 + Google dashboard: 成本、吞吐和趋势一起看。",
                "detail": "累计 {}，{} tokens，{} 消息；cache read {}，cache write {}。".format(
                    _fmt_money(lifetime["cost"], 0),
                    _compact(lifetime["tokens"]),
                    _compact(lifetime["messages"]),
                    _pct(lifetime["cache_read"], lifetime["tokens"]),
                    _pct(lifetime["cache_write"], lifetime["tokens"]),
                ),
            },
            {
                "id": "adoption_coverage",
                "family": "Tesla fleet telemetry",
                "label": "覆盖与采集健康",
                "value": "{} 人 · {} 部门 · {} 工具".format(
                    _fmt_int(lifetime["users"]), _fmt_int(lifetime["depts"]), _fmt_int(lifetime["clients"])),
                "status": "computed",
                "availability": "computed",
                "benchmark": "Tesla fleet 思路: 先确认哪些终端/工具已接入，再解释趋势。",
                "detail": "近 7 天活跃 {} 人，source Top: {}；工具 Top: {}。".format(
                    _fmt_int(last7["users"]), _top_names(sources, "source"), _top_names(clients, "client")),
            },
            {
                "id": "code_acceptance",
                "family": "AI coding output",
                "label": "代码采纳与有效行",
                "value": "Cursor {} 人".format(_fmt_int(cursor["users"])),
                "status": "partial",
                "availability": "partial",
                "benchmark": "Cursor Admin API + Claude Code OTEL + git survival 可进入同一 code_daily 指标族。",
                "detail": "当前可算 Cursor 覆盖与 token 使用量({} tokens)；accepted lines、survival lines 还未入库。".format(
                    _compact(cursor["tokens"])),
            },
            {
                "id": "delivery_quality",
                "family": "Google/DORA throughput",
                "label": "交付质量",
                "value": "待接入 CI/CD",
                "status": "pending",
                "availability": "pending",
                "benchmark": "Google/DORA: change lead time、deployment frequency、change fail rate、MTTR。",
                "detail": "现有库没有发布、PR、CI、事故恢复时间，暂不能计算 DORA 指标。",
            },
            {
                "id": "reliability_budget",
                "family": "Google SRE",
                "label": "可靠性与错误预算",
                "value": freshness,
                "status": "partial",
                "availability": "partial",
                "benchmark": "Google SRE: dashboard 应回答核心健康问题，error budget 平衡稳定和创新。",
                "detail": "当前可算数据新鲜度、日期跨度({} 天)与日粒度行数({})；正式 SLO/error budget 还需 API 错误率和同步失败率。".format(
                    _fmt_int(day["days"]), _fmt_int(day["rows"])),
            },
            {
                "id": "privacy_purpose",
                "family": "Meta Policy Zones · Tesla Data Sharing",
                "label": "隐私与目的限制",
                "value": "聚合计数",
                "status": "computed",
                "availability": "computed",
                "benchmark": "Meta Policy Zones 强调 purpose limitation；Tesla Data Sharing 强调用户可控和最小化。",
                "detail": "usage schema 只保存 email、部门、工具、模型、token、成本、日期等聚合字段，不保存 prompt 或代码正文。",
            },
            {
                "id": "collection_health",
                "family": "Telemetry operations",
                "label": "采集链路健康",
                "value": "{} 上报 · {} 设备".format(
                    _fmt_int(report_log["reports"]), _fmt_int(report_log["devices"])),
                "status": "partial",
                "availability": "partial",
                "benchmark": "事件总线/缓冲队列模式要求监控 ingest 成功率、重试、延迟和去重。",
                "detail": "当前 report_log 可看最近上报({})、设备数和手工补报({})；失败重试/延迟分布还未采集。Agent key 覆盖 {} 个。".format(
                    report_log["last_report"] or "暂无",
                    _fmt_int(report_log["manual_reports"]),
                    _fmt_int(agent["users"])),
            },
        ]

        self._send(200, {
            "metrics": metrics,
            "summary": {
                "lifetime": lifetime,
                "day": day,
                "last7": last7,
                "report_log": report_log,
                "subscriptions": {
                    "unresolved": _num(subs_unresolved),
                    "idle": idle_subscriptions,
                },
                "sources": sources,
                "clients": clients,
            },
        })

    def _meta(self, conn):
        """数据真实日期跨度 + 最后上报时间（看板默认渲染时间范围用）。
        日期来自日粒度行(period_type='day')，即看板能按区间过滤的真实窗口。"""
        row = conn.execute(
            "SELECT MIN(period), MAX(period) FROM usage WHERE period_type='day'"
        ).fetchone()
        last = conn.execute(
            "SELECT MAX(reported_at) FROM report_log").fetchone()
        self._send(200, {
            "min_date": (row[0] if row else "") or "",
            "max_date": (row[1] if row else "") or "",
            "last_report": (last[0] if last else "") or "",
        })

    def _raw(self, conn):
        """明细（调试用，LIMIT 100）。"""
        rows = conn.execute("""
            SELECT email, period_type, period, source, client, provider, model,
                   input, output, cache_read, cache_write, reasoning, total, cost, messages
            FROM usage ORDER BY total DESC LIMIT 100
        """).fetchall()
        cols = ["email", "period_type", "period", "source", "client", "provider", "model",
                "input", "output", "cache_read", "cache_write", "reasoning", "total", "cost", "messages"]
        self._send(200, {"rows": [dict(zip(cols, r)) for r in rows]})


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    bind = os.environ.get("BIND_HOST", "0.0.0.0")
    sys.stderr.write(
        "dev_collector on {host}:{port}  db={db}  tokens={n}\n".format(
            host=bind, port=PORT, db=DB, n=len(TOKENS)
        )
    )
    ThreadingHTTPServer((bind, PORT), H).serve_forever()
