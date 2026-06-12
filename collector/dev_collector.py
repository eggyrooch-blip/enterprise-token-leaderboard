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
    # 飞书 AI 权益(独立三表,单位=「点」credits,与 token 不加总;一周期一快照,主键覆盖)
    c.execute("""CREATE TABLE IF NOT EXISTS feishu_member(
        email TEXT NOT NULL, name TEXT DEFAULT '', dept TEXT DEFAULT 'unknown',
        feature_key TEXT NOT NULL, credits REAL NOT NULL DEFAULT 0,
        period_start TEXT NOT NULL, period_end TEXT DEFAULT '',
        avatar TEXT DEFAULT '', entity_id TEXT DEFAULT '',
        PRIMARY KEY(email, feature_key, period_start))""")
    c.execute("""CREATE TABLE IF NOT EXISTS feishu_quota(
        feature_key TEXT NOT NULL, quota REAL DEFAULT 0, used REAL DEFAULT 0,
        remain REAL DEFAULT 0, period_start TEXT NOT NULL, period_end TEXT DEFAULT '',
        PRIMARY KEY(feature_key, period_start))""")
    c.execute("""CREATE TABLE IF NOT EXISTS feishu_trend(
        usage_date TEXT NOT NULL, biz_type TEXT NOT NULL, biz_name TEXT DEFAULT '',
        credits REAL DEFAULT 0, user_count INTEGER DEFAULT 0,
        PRIMARY KEY(usage_date, biz_type))""")
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


_CLIENT_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex CLI",
    "gemini": "Gemini CLI",
    "cursor": "Cursor",
    "opencode": "OpenCode",
    "kimi": "Kimi CLI",
}

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


def _show_departed(qs):
    """解析 ?show_departed=1 → bool。1/true/yes 视为 True，其余 False。"""
    raw = (qs.get("show_departed") or [None])[0]
    return str(raw).strip().lower() in ("1", "true", "yes")


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
            conn.execute(
                "INSERT OR REPLACE INTO feishu_member"
                "(email,name,dept,feature_key,credits,period_start,period_end,avatar,entity_id)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (m.get("email") or "", m.get("name") or "", m.get("dept") or "unknown",
                 m.get("feature_key") or "", float(m.get("credits") or 0), ps, pe,
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
            if path == "/v1/meta":
                return self._meta(conn)
            if path == "/v1/governance_metrics":
                return self._governance_metrics(conn)
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
                    "GET  /v1/governance_metrics     (大厂治理指标可计算项)",
                    "GET  /v1/raw",
                ],
            })
        finally:
            conn.close()

    def _feishu(self, conn, qs):
        """飞书 AI 权益(独立板块,单位=点)。返回最新周期快照:额度盘 + 全员逐人榜
        + 部门榜 + 趋势。?show_departed=1 才纳入离职。"""
        period = conn.execute("SELECT max(period_start) FROM feishu_member").fetchone()[0]
        if not period:
            return self._send(200, {"period_start": None, "quota": [], "members": [],
                                    "dept": [], "trend": []})
        pe = (conn.execute("SELECT max(period_end) FROM feishu_member WHERE period_start=?",
                           (period,)).fetchone() or [""])[0]
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
                       " FROM feishu_member WHERE period_start=?%s"
                       " GROUP BY email HAVING SUM(credits)>0 ORDER BY SUM(credits) DESC" % dep,
                       (period,)).fetchall()]
        dept = [{"dept": r[0] or "unknown", "credits": r[1] or 0, "people": r[2] or 0}
                for r in conn.execute(
                    "SELECT dept, SUM(credits), COUNT(DISTINCT email) FROM feishu_member"
                    " WHERE period_start=?%s GROUP BY dept ORDER BY SUM(credits) DESC" % dep,
                    (period,)).fetchall()]
        trend = [{"usage_date": r[0], "biz_type": r[1], "credits": r[2] or 0, "user_count": r[3] or 0}
                 for r in conn.execute(
                     "SELECT usage_date,biz_type,credits,user_count FROM feishu_trend"
                     " ORDER BY usage_date").fetchall()]
        self._send(200, {"period_start": period, "period_end": pe,
                         "quota": quota, "members": members, "dept": dept, "trend": trend})

    def _leaderboard(self, conn, qs):
        """按人聚合(区间 ?days=N 或全部),join people 取中文姓名+头像+完整部门路径。
        同一人当天 Cursor+Claude+Codex 的 token 自动求和(GROUP BY email)。"""
        where, params = _range_clause(qs, "u.")
        sd = _show_departed(qs)
        dep_clause = _departed_filter(sd, "u.")
        departed = _departed_set(conn)
        # 可选 ?client=Claude Code|Codex CLI|... → 只统计该工具(Claude 榜 / Codex 榜复用此端点)
        cli = (qs.get("client") or [None])[0]
        cli_clause = " AND u.client = ?" if cli else ""
        params2 = list(params) + ([cli] if cli else [])
        # agent key 用量(source=litellm_agent)不进个人榜 —— 单独走 /v1/agent_leaderboard
        rows = conn.execute("""
            SELECT u.email, MAX(u.dept),
                   SUM(u.input), SUM(u.output), SUM(u.cache_read), SUM(u.cache_write),
                   SUM(u.reasoning), SUM(u.total), SUM(u.cost), SUM(u.messages),
                   MAX(p.name), MAX(p.avatar),
                   (SELECT rl.via FROM report_log rl WHERE rl.email = u.email
                    ORDER BY rl.reported_at DESC LIMIT 1),
                   MAX(p.dept)
            FROM usage u LEFT JOIN people p ON p.email = u.email
            WHERE %s AND u.source != 'litellm_agent'%s%s
            GROUP BY u.email
            HAVING SUM(u.total) > 0
            ORDER BY SUM(u.total) DESC
        """ % (where, dep_clause, cli_clause), params2).fetchall()
        # 每人按工具(client)的构成:Claude/Codex/Cursor/Gemini/... 占比
        comp = {}
        for cr in conn.execute("""
            SELECT u.email, u.client, SUM(u.total)
            FROM usage u
            WHERE %s AND u.source != 'litellm_agent'%s%s
            GROUP BY u.email, u.client
        """ % (where, dep_clause, cli_clause), params2).fetchall():
            comp.setdefault(cr[0], []).append({"client": cr[1], "tokens": cr[2] or 0})
        result = []
        for r in rows:
            total = r[7] or 0
            parts = sorted(comp.get(r[0], []), key=lambda x: x["tokens"], reverse=True)
            for x in parts:
                x["pct"] = round(x["tokens"] / total * 100, 1) if total else 0
            # 部门优先用 people.dept(飞连自愈的全路径),裸的 MAX(u.dept) 只兜底 ——
            # 否则 LiteLLM 团队别名(裸"技术平台部")会被 MAX 误选盖掉飞连全路径(中文排在 'K' 之后)。
            result.append({
                "email": r[0], "dept": _to_keep(r[13]) or _normalize_dept_path(r[1]),
                "input": r[2] or 0, "output": r[3] or 0,
                "cache_read": r[4] or 0, "cache_write": r[5] or 0,
                "reasoning": r[6] or 0, "tokens": total,
                "cost": round(r[8] or 0, 4), "messages": r[9] or 0,
                "name": r[10] or (r[0] or "").split("@")[0],
                "avatar": r[11] or "",
                "via": r[12] or "",   # 最近一次订阅制上报来源:manual=手工补报(看板打角标)
                "departed": r[0] in departed,
                "composition": parts,
            })
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
        for r in rows:
            result.append({
                "email": r[0], "dept": _to_keep(r[12]) or _normalize_dept_path(r[1]),  # 优先 people.dept(飞连全路径)
                "input": r[2] or 0, "output": r[3] or 0,
                "cache_read": r[4] or 0, "cache_write": r[5] or 0,
                "reasoning": r[6] or 0, "tokens": r[7] or 0,
                "cost": round(r[8] or 0, 2), "requests": r[9] or 0,
                "name": r[10] or (r[0] or "").split("@")[0], "avatar": r[11] or "",
                "departed": r[0] in departed,
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

        # aily(飞书 AI 权益)并入部门榜:取最新一个周期快照(与 token 的 7/30 天筛选解耦)。
        # 单位「点」credits,不与 token 加总;aily 的人并进活跃集 → 活跃渗透取并集。
        period = (conn.execute("SELECT max(period_start) FROM feishu_member").fetchone()
                  or [None])[0]
        if period:
            fdep = "" if sd else " AND email NOT IN (SELECT email FROM departed)"
            aily_rows = conn.execute(
                "SELECT email, MAX(dept), SUM(credits) FROM feishu_member"
                " WHERE period_start=?%s GROUP BY email HAVING SUM(credits)>0" % fdep,
                (period,)).fetchall()
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

    def _governance_metrics(self, conn):
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
