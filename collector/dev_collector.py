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
import unicodedata
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
LEADERBOARD_EXCLUDE_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("LEADERBOARD_EXCLUDE_EMAILS", "sunke@keep.com").split(",")
    if e.strip()
}
AUTH_SUPER_ADMIN_EMAILS = {"sunke@keep.com"}

BUCKET_EMPLOYEE = "employee_staff_outsourcing"
BUCKET_BUSINESS = "business_outsourcing"
BUCKET_PENDING_BUSINESS = "pending_business_outsourcing"
BUCKET_UNRESOLVED = "unresolved"
VISIBLE_SPEND_BUCKETS = {BUCKET_EMPLOYEE, BUCKET_BUSINESS}

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
    _ensure_people_directory_columns(c)
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
    _ensure_usage_attribution_columns(c)
    ensure_auth_tables(c)
    _ensure_app_state_table(c)
    if _usage_backfill_needed(c):
        _backfill_usage_attribution(c, dry_run=False)
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
def _table_columns(conn, table):
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    except Exception:
        return set()


def _add_column_if_missing(conn, table, column, ddl):
    if column not in _table_columns(conn, table):
        conn.execute("ALTER TABLE %s ADD COLUMN %s" % (table, ddl))


def _ensure_usage_attribution_columns(conn):
    if "usage" not in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }:
        return
    _add_column_if_missing(conn, "usage", "raw_dept", "raw_dept TEXT DEFAULT ''")
    _add_column_if_missing(conn, "usage", "effective_dept", "effective_dept TEXT DEFAULT ''")
    _add_column_if_missing(conn, "usage", "spend_bucket", "spend_bucket TEXT DEFAULT '%s'" % BUCKET_EMPLOYEE)
    _add_column_if_missing(conn, "usage", "attribution_source", "attribution_source TEXT DEFAULT ''")


def _ensure_people_directory_columns(conn):
    if "people" not in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }:
        return
    _add_column_if_missing(conn, "people", "feishu_user_id", "feishu_user_id TEXT DEFAULT ''")
    _add_column_if_missing(conn, "people", "feishu_open_id", "feishu_open_id TEXT DEFAULT ''")
    _add_column_if_missing(conn, "people", "status", "status TEXT DEFAULT 'active'")
    _add_column_if_missing(conn, "people", "source", "source TEXT DEFAULT ''")
    _add_column_if_missing(conn, "people", "raw_dept", "raw_dept TEXT DEFAULT ''")
    _add_column_if_missing(conn, "people", "effective_dept", "effective_dept TEXT DEFAULT ''")
    _add_column_if_missing(conn, "people", "attribution_source", "attribution_source TEXT DEFAULT ''")
    _add_column_if_missing(conn, "people", "spend_bucket", "spend_bucket TEXT DEFAULT '%s'" % BUCKET_EMPLOYEE)
    _add_column_if_missing(conn, "people", "updated_at", "updated_at TEXT DEFAULT ''")


def ensure_auth_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS roles(
        email TEXT NOT NULL,
        role TEXT NOT NULL,
        dept_id TEXT DEFAULT '',
        dept_path TEXT DEFAULT '',
        source TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(email, role, dept_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS role_overrides(
        email TEXT NOT NULL,
        role TEXT NOT NULL,
        dept_id TEXT DEFAULT '',
        action TEXT NOT NULL,
        reason TEXT DEFAULT '',
        PRIMARY KEY(email, role, dept_id, action))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS auth_states(
        state TEXT PRIMARY KEY,
        redirect TEXT,
        created_at INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS auth_sessions(
        sid TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        created_at INTEGER,
        expires_at INTEGER,
        last_seen_at INTEGER)""")


def _ensure_app_state_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS app_state(
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT '')""")


def _state_get(conn, key, default=""):
    try:
        row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    except Exception:
        return default
    return row[0] if row else default


def _state_set(conn, key, value):
    _ensure_app_state_table(conn)
    conn.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES(?,?)", (key, value))


def _state_bool(v):
    return _sstr(v).strip().lower() in ("1", "true", "yes", "on")


def _state_float(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _state_int(v):
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _state_json_list(v):
    try:
        data = json.loads(v or "[]")
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _feishu_sync_health(conn):
    warnings = _state_json_list(
        _state_get(conn, "feishu_directory_sync_visibility_warnings", "[]"))
    return {
        "status": _state_get(conn, "feishu_directory_sync_status", "unknown"),
        "last_success": _state_get(conn, "feishu_directory_sync_last_success", ""),
        "last_attempt": _state_get(conn, "feishu_directory_sync_last_attempt", ""),
        "last_error": _state_get(conn, "feishu_directory_sync_last_error", ""),
        "visibility_warnings": warnings,
        "visibility_warnings_count": len(warnings),
        "production_enablement_blocked": _state_bool(
            _state_get(conn, "feishu_directory_sync_production_enablement_blocked", "0")),
        "business_rollup_enabled": _state_bool(
            _state_get(conn, "feishu_directory_sync_business_rollup_enabled", "0")),
        "resolved_business_outsourcing_rate": _state_float(
            _state_get(conn, "feishu_directory_sync_resolved_business_outsourcing_rate", "0")),
        "min_required_rate": _state_float(
            _state_get(conn, "feishu_directory_sync_min_required_rate", "0")),
        "users": _state_int(_state_get(conn, "feishu_directory_sync_users", "0")),
        "departments": _state_int(_state_get(conn, "feishu_directory_sync_departments", "0")),
        "supplier_departments": _state_int(
            _state_get(conn, "feishu_directory_sync_supplier_departments", "0")),
        "unresolved": _state_int(_state_get(conn, "feishu_directory_sync_unresolved", "0")),
    }


def _redacted_feishu_sync_health(sync):
    return {
        "status": sync.get("status", "unknown"),
        "last_success": sync.get("last_success", ""),
        "last_attempt": sync.get("last_attempt", ""),
        "visibility_warnings_count": int(sync.get("visibility_warnings_count") or 0),
        "production_enablement_blocked": bool(sync.get("production_enablement_blocked")),
        "business_rollup_enabled": bool(sync.get("business_rollup_enabled")),
        "resolved_business_outsourcing_rate": float(
            sync.get("resolved_business_outsourcing_rate") or 0.0),
        "min_required_rate": float(sync.get("min_required_rate") or 0.0),
    }


def _configured_admin_emails():
    emails = set(AUTH_SUPER_ADMIN_EMAILS)
    raw = os.environ.get("AUTH_ADMIN_EMAILS", "")
    for item in raw.split(","):
        email = item.strip().lower()
        if email:
            emails.add(email)
    return emails


def _role_override_rows(conn, email):
    try:
        return conn.execute(
            "SELECT role, COALESCE(dept_id,''), action FROM role_overrides WHERE lower(email)=?",
            ((email or "").lower(),),
        ).fetchall()
    except Exception:
        return []


def _role_denied(overrides, role, dept_id=""):
    did = dept_id or ""
    return any(r == role and action == "deny" and ((odid or "") in ("", did))
               for r, odid, action in overrides)


def _dept_path_for_override(conn, dept_id):
    dept_id = (dept_id or "").strip()
    if not dept_id:
        return ""
    if "/" in dept_id:
        return dept_id
    try:
        row = conn.execute(
            "SELECT COALESCE(path, '') FROM departments WHERE dept_id=?",
            (dept_id,),
        ).fetchone()
    except Exception:
        return ""
    return row[0] if row and row[0] else ""


def _user_roles(conn, email):
    email = (email or "").strip().lower()
    overrides = _role_override_rows(conn, email)
    roles = set()
    owned = set()

    is_super_admin = email in AUTH_SUPER_ADMIN_EMAILS
    if is_super_admin:
        roles.add("admin")
    elif email in _configured_admin_emails() and not _role_denied(overrides, "admin"):
        roles.add("admin")

    try:
        rows = conn.execute(
            "SELECT role, COALESCE(dept_id,''), COALESCE(dept_path,'')"
            " FROM roles WHERE lower(email)=?",
            (email,),
        ).fetchall()
    except Exception:
        rows = []
    for role, dept_id, dept_path in rows:
        if _role_denied(overrides, role, dept_id):
            continue
        if role:
            roles.add(role)
        if role == "department_owner" and dept_path:
            owned.add(dept_path)

    for role, dept_id, action in overrides:
        if action == "allow" and not _role_denied(overrides, role, dept_id):
            if role == "department_owner":
                dept_path = _dept_path_for_override(conn, dept_id)
                if not dept_path:
                    continue
                owned.add(dept_path)
            roles.add(role)

    if "admin" in roles:
        scope = "global"
    elif "department_owner" in roles:
        scope = "department"
    else:
        roles.add("member")
        scope = "self"
    return {
        "email": email,
        "roles": sorted(roles),
        "is_admin": "admin" in roles,
        "scope": scope,
        "owned_departments": sorted(owned),
    }


# ===========================================================================
# Feishu OAuth + session auth (shadow-first; gated by AUTH_ENFORCE)
# ---------------------------------------------------------------------------
# Authentication (who you are) is always available: /v1/auth/login, /callback,
# /logout, /v1/me work regardless of AUTH_ENFORCE. Authorization (what you can
# see) is gated by AUTH_ENFORCE so production can run in shadow mode (=0) until
# one real admin login is verified, then flip to enforce (=1). The machine
# report path (/v1/tokscale/report, /tokreport.*) NEVER uses sessions — it keeps
# its bearer-token auth.
# ===========================================================================
import secrets as _secrets
from urllib.parse import urlencode as _urlencode
from urllib.request import Request as _Request, urlopen as _urlopen

FEISHU_HOST = os.environ.get("FEISHU_HOST", "https://open.feishu.cn").rstrip("/")
FEISHU_AUTH_HOST = os.environ.get("FEISHU_AUTH_HOST", "https://accounts.feishu.cn").rstrip("/")
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
FEISHU_OAUTH_REDIRECT_URI = os.environ.get("FEISHU_OAUTH_REDIRECT_URI", "").strip()

SESSION_COOKIE = "tok_auth"
SESSION_TTL = int(os.environ.get("AUTH_SESSION_TTL", str(7 * 24 * 3600)))
STATE_TTL = int(os.environ.get("AUTH_STATE_TTL", "600"))


def _default_auth_cookie_secure():
    return FEISHU_OAUTH_REDIRECT_URI.lower().startswith("https://")


AUTH_COOKIE_SECURE = os.environ.get(
    "AUTH_COOKIE_SECURE",
    "1" if _default_auth_cookie_secure() else "0",
).strip() == "1"

# Data routes guarded by AUTH_ENFORCE. The report/static/auth routes are NOT here.
DATA_ROUTES = {
    "/v1/leaderboard", "/v1/agent_leaderboard", "/v1/agent_owner_summary",
    "/v1/teams", "/v1/cursor", "/v1/breakdown", "/v1/trend", "/v1/ai/usage",
    "/v1/meta", "/v1/governance_metrics", "/v1/feishu", "/v1/raw",
}
# Route authorization categories (only consulted when AUTH_ENFORCE=1):
#  - ADMIN_ONLY: company-wide aggregates that can't be row-scoped to an
#    individual in v1 -> non-admin gets 403.
#  - OWNER_OR_ADMIN: member 403; department_owner sees own subtree (row-filtered).
#  - everything else in DATA_ROUTES is ROW-SCOPED: the handler filters rows
#    through _scope_rows() / email_in_scope() to the caller's visible set.
ADMIN_ONLY_ROUTES = {
    "/v1/governance_metrics", "/v1/raw",
    "/v1/agent_leaderboard", "/v1/agent_owner_summary",
}
OWNER_OR_ADMIN_ROUTES = {"/v1/teams"}


def auth_enforced():
    """Read at call-time so tests / ops can toggle AUTH_ENFORCE without reload."""
    return os.environ.get("AUTH_ENFORCE", "0").strip() == "1"


def _oauth_http_json(url, payload=None, headers=None):
    """Thin JSON HTTP helper for the OAuth dance. Monkeypatched in tests."""
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json; charset=utf-8")
    req = _Request(url, data=data, headers=req_headers)
    resp = _urlopen(req, timeout=15)
    raw = resp.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw or "{}")


def feishu_authorize_url(state):
    """The Feishu authorize URL the browser is 302'd to at login."""
    q = _urlencode({
        "client_id": FEISHU_APP_ID,
        "redirect_uri": FEISHU_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "state": state,
    })
    return FEISHU_AUTH_HOST + "/open-apis/authen/v1/authorize?" + q


def _safe_next_path(raw):
    raw = (raw or "").strip()
    if not raw:
        return "/"
    try:
        p = urlparse(raw)
    except Exception:
        return "/"
    if p.scheme or p.netloc:
        return "/"
    path = p.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return "/"
    if path.startswith("/v1/auth/callback"):
        return "/"
    return path + (("?" + p.query) if p.query else "")


def feishu_exchange_code(code):
    """Exchange an OAuth code for the user's profile.

    Returns ``{email, name, open_id}`` (email lowercased, may be empty). Network
    goes through :func:`_oauth_http_json` so tests inject fakes. A live round-trip
    is still required before flipping ``AUTH_ENFORCE=1`` in production.
    """
    tok = _oauth_http_json(
        FEISHU_HOST + "/open-apis/authen/v2/oauth/token",
        {"grant_type": "authorization_code", "client_id": FEISHU_APP_ID,
         "client_secret": FEISHU_APP_SECRET, "code": code,
         "redirect_uri": FEISHU_OAUTH_REDIRECT_URI})
    if tok.get("code") not in (None, 0):
        raise RuntimeError(tok.get("msg") or tok.get("error") or "token exchange failed")
    tdata = tok.get("data") or {}
    user_token = (
        tok.get("user_access_token")
        or tok.get("access_token")
        or tdata.get("user_access_token")
        or tdata.get("access_token")
        or ""
    )
    if not user_token:
        raise RuntimeError("token exchange returned empty user_access_token")
    info = _oauth_http_json(
        FEISHU_HOST + "/open-apis/authen/v1/user_info", None,
        {"Authorization": "Bearer " + user_token})
    d = info.get("data") or info or {}
    return {
        "email": (d.get("email") or d.get("enterprise_email") or "").strip().lower(),
        "name": d.get("name") or "",
        "open_id": d.get("open_id") or "",
    }


def create_oauth_state(conn, redirect="/", now=None):
    now = int(now if now is not None else time.time())
    state = _secrets.token_urlsafe(24)
    conn.execute("INSERT OR REPLACE INTO auth_states(state,redirect,created_at)"
                 " VALUES(?,?,?)", (state, redirect or "/", now))
    conn.commit()
    return state


def consume_oauth_state(conn, state, now=None):
    """One-time-use + expiry. Returns the stored redirect, or None if invalid."""
    if not state:
        return None
    now = int(now if now is not None else time.time())
    row = conn.execute(
        "SELECT redirect, created_at FROM auth_states WHERE state=?", (state,)
    ).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM auth_states WHERE state=?", (state,))  # consume once
    conn.commit()
    if now - int(row[1] or 0) > STATE_TTL:
        return None
    return row[0] or "/"


def create_session(conn, email, now=None):
    now = int(now if now is not None else time.time())
    sid = _secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO auth_sessions(sid,email,created_at,expires_at,last_seen_at)"
        " VALUES(?,?,?,?,?)",
        (sid, (email or "").strip().lower(), now, now + SESSION_TTL, now))
    conn.commit()
    return sid


def session_email(conn, sid, now=None):
    if not sid:
        return None
    now = int(now if now is not None else time.time())
    row = conn.execute(
        "SELECT email, expires_at FROM auth_sessions WHERE sid=?", (sid,)).fetchone()
    if not row:
        return None
    if row[1] and now > int(row[1]):
        conn.execute("DELETE FROM auth_sessions WHERE sid=?", (sid,))
        conn.commit()
        return None
    conn.execute("UPDATE auth_sessions SET last_seen_at=? WHERE sid=?", (now, sid))
    conn.commit()
    return row[0]


def delete_session(conn, sid):
    if sid:
        conn.execute("DELETE FROM auth_sessions WHERE sid=?", (sid,))
        conn.commit()


def email_in_scope(roleinfo, row_email, row_dept_path=""):
    """Pure row-visibility predicate for a (person email, dept path) under a role.

    admin -> everything; member -> only self; department_owner -> own dept
    subtree (by canonical dept key prefix) plus self.
    """
    if not roleinfo:
        return False
    if roleinfo.get("is_admin"):
        return True
    me = (roleinfo.get("email") or "").strip().lower()
    if (row_email or "").strip().lower() == me:
        return True
    if roleinfo.get("scope") == "department":
        rp = _canonical_dept_key(row_dept_path)
        if not rp:
            return False
        for owned in roleinfo.get("owned_departments", []):
            ok = _canonical_dept_key(owned)
            if ok and (rp == ok or rp.startswith(ok + "/")):
                return True
    return False


def _scope_dept(email, dept, pdept=None, effective_dept=""):
    if effective_dept:
        trusted = _trusted_keep_path(effective_dept)
        if trusted:
            return trusted
    if pdept:
        cand = _to_keep(pdept.get(email))
        if cand:
            return cand
    return _to_keep(dept) or _trusted_keep_path(dept) or dept or ""


def filter_person_rows_for_auth(rows, auth_user):
    if not auth_user or auth_user.get("is_admin"):
        return rows
    return [
        r for r in rows
        if email_in_scope(auth_user, r.get("email") or r.get("user"), r.get("dept") or "")
    ]


def authorize_request(user, path, enforced):
    """Pure endpoint-level authorization decision.

    Returns 'allow' | '401' | '403'. Row-level scoping (member self-rows / owner
    subtree) lives in each handler; endpoint-level auth only blocks routes that
    cannot yet be scoped safely. `/v1/me` and the auth/report/static routes are
    never gated here.
    """
    if not enforced or path not in DATA_ROUTES:
        return "allow"
    if user is None:
        return "401"
    if user.get("is_admin"):
        return "allow"
    if path in ADMIN_ONLY_ROUTES:
        return "403"
    if path in OWNER_OR_ADMIN_ROUTES and user.get("scope") == "self":
        return "403"  # plain members have no team view
    # Row-scoped route: allow through; the handler filters rows to scope.
    return "allow"


def num(r, *keys):
    """从 dict r 中按 keys 顺序取第一个非 None 整数，失败返回 0。"""
    for k in keys:
        if k in r and r[k] is not None:
            v = _unwrap(r[k])          # list/dict 形状先剥成标量,保住数值不被零掉
            try:
                return int(v)
            except (TypeError, ValueError, OverflowError):
                try:
                    f = float(v)
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


def _unwrap(v):
    """把畸形上报字段拆成单个标量。

    实测某 tokscale 客户端(Windows)把本该是标量的字段发成单元素 list,例如
    client=['claude'] / provider=['anthropic'] / model=['x'] / cost=[1.2] /
    month=['2026-06'] / date=['2026-06-17']。这些值若直接进 dict.get(→unhashable)、
    直接 bind 进 SQLite(→InterfaceError)或喂给 float()(→TypeError),都会把整份
    上报打成 500。这里在每个标量字段入库前先剥成标量(list→首个非空元素,dict→
    常见键),从源头消灭"任意字段是 list"这一类 500。
    """
    seen = 0
    while isinstance(v, (list, tuple)) and seen < 5:
        v = next((x for x in v if x not in (None, "")), None)
        seen += 1
    if isinstance(v, dict):
        v = v.get("id") or v.get("name") or v.get("value") or ""
    return v


def _clean_serial(raw):
    """从可能畸形的 serial 字段里取出真正的序列号。

    实测 Windows PowerShell 客户端有 bug:Log 函数用 Write-Output,泄漏进
    Get-DeviceSerial 的返回值,使 serial 变成一个 list —— 日志行 + 真 SN 混在一起,例如
      ['[tokreport-windows] ... BIOS is <SN>', '... baseboard is <BB>',
       '... The meaningful SN should be <SN> ...', '<SN>']
    旧代码 `serial in _serial_cache` 直接拿 list 当 key → unhashable → 整份上报 500
    (这才是 Windows 机器一直进不了榜的真根因)。这里把 list 里日志行(都含空白)滤掉,
    取最后一个无空白的候选作真 SN;客户端侧另有修复让 serial 不再被污染。
    """
    if isinstance(raw, (list, tuple)):
        cands = [str(x).strip() for x in raw if str(x).strip()]
        nospace = [c for c in cands if len(c.split()) == 1]   # 真 SN 无空白;日志行有空格
        raw = nospace[-1] if nospace else (cands[-1] if cands else "")
    elif isinstance(raw, dict):
        raw = str(raw.get("id") or raw.get("value") or "")
    elif not isinstance(raw, str):
        raw = "" if raw is None else str(raw)
    return raw.strip()


def _sstr(v, default=""):
    """归一为可安全写库的字符串(防 list/dict 直接 bind SQLite 抛 InterfaceError)。"""
    v = _unwrap(v)
    if v is None:
        return default
    return v if isinstance(v, str) else str(v)


def _sfloat(v):
    """归一为 float;任何畸形(list/dict/None/非数字/NaN/inf)→ 0.0。"""
    v = _unwrap(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f or f in (float("inf"), float("-inf")):
        return 0.0
    return f


def _client_label(raw):
    """把 client 字段归一为展示标签(先剥标量再查表,防 unhashable)。"""
    raw = _unwrap(raw)
    if not isinstance(raw, str):
        raw = "" if raw is None else str(raw)
    raw = raw.strip() or "unknown"
    return _CLIENT_LABELS.get(raw, raw)


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


def _upsert_lifetime(conn, email, dept, entries, identity=None):
    """将 tokscale models --json entries UPSERT 为 period_type=lifetime。"""
    up = 0
    identity = identity or _attribution_for_raw_dept(conn, dept)
    for e in entries:
        client = _client_label(e.get("client", "unknown"))
        provider = _sstr(e.get("provider"))
        model = _sstr(e.get("model")) or "unknown"
        inp = num(e, "input")
        out = num(e, "output")
        cr = num(e, "cacheRead")
        cw = num(e, "cacheWrite")
        reasoning = num(e, "reasoning")
        total = inp + out + cr + cw + reasoning
        cost = _sfloat(e.get("cost"))
        messages = num(e, "messageCount")
        conn.execute(_UPSERT_SQL, (
            email, dept, "lifetime", "all", "subscription",
            client, provider, model,
            inp, out, cr, cw, reasoning, total, cost, messages,
        ))
        _set_usage_attribution(conn, email, "lifetime", "all", "subscription",
                               client, provider, model, identity)
        up += 1
    return up


def _upsert_monthly(conn, email, dept, entries, identity=None):
    """将 tokscale monthly --json entries UPSERT 为 period_type=month。

    monthly 格式: {month, models(list), input, output, cacheRead, cacheWrite,
                   messageCount, cost}
    无 provider/reasoning/client — 存为空字符串/0，client 固定 '__monthly__'。
    """
    up = 0
    identity = identity or _attribution_for_raw_dept(conn, dept)
    for e in entries:
        month = _sstr(e.get("month"))
        if not month:
            continue
        inp = num(e, "input")
        out = num(e, "output")
        cr = num(e, "cacheRead")
        cw = num(e, "cacheWrite")
        reasoning = num(e, "reasoning")          # monthly 通常无此字段 → 0
        total = inp + out + cr + cw + reasoning
        cost = _sfloat(e.get("cost"))
        messages = num(e, "messageCount")
        # provider 必须用稳定常量：之前塞乱序模型列表 → 每次跑主键都不同 → 月度翻倍。
        # 月度只做时间桶,模型维度从 lifetime 行取,这里 provider 固定为空。
        conn.execute(_UPSERT_SQL, (
            email, dept, "month", month, "subscription",
            "__monthly__", "", "__aggregated__",
            inp, out, cr, cw, reasoning, total, cost, messages,
        ))
        _set_usage_attribution(conn, email, "month", month, "subscription",
                               "__monthly__", "", "__aggregated__", identity)
        up += 1
    return up


def _upsert_daily(conn, email, dept, graph, identity=None):
    """将 tokscale graph 的 contributions[] 落为 period_type='day' 日桶(每天每模型 token)。
    graph: {contributions:[{date:'YYYY-MM-DD', clients:[{client,modelId,providerId,
            tokens:{input,output,cacheRead,cacheWrite,reasoning}, cost, messages}]}]}"""
    up = 0
    identity = identity or _attribution_for_raw_dept(conn, dept)
    for d in (graph or {}).get("contributions") or []:
        day = _sstr(d.get("date"))
        if not day:
            continue
        for c in d.get("clients") or []:
            tk = c.get("tokens") or {}
            client = _client_label(c.get("client", "unknown"))
            inp = num(tk, "input"); out = num(tk, "output")
            cr = num(tk, "cacheRead"); cw = num(tk, "cacheWrite"); rs = num(tk, "reasoning")
            total = inp + out + cr + cw + rs
            conn.execute(_UPSERT_SQL, (
                email, dept, "day", day, "subscription",
                client, _sstr(c.get("providerId")), _sstr(c.get("modelId")) or "unknown",
                inp, out, cr, cw, rs, total, _sfloat(c.get("cost")), num(c, "messages"),
            ))
            _set_usage_attribution(conn, email, "day", day, "subscription",
                                   client, _sstr(c.get("providerId")), _sstr(c.get("modelId")) or "unknown",
                                   identity)
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


def _include_excluded(qs):
    """解析 ?include_excluded=1 → bool。只影响 LEADERBOARD_EXCLUDE_EMAILS。"""
    raw = (qs.get("include_excluded") or [None])[0]
    return str(raw).strip().lower() in ("1", "true", "yes")


def _excluded_filter(qs, prefix=""):
    """配置化极值/内部账号过滤。默认生效;?include_excluded=1 时关闭。

    返回 (sql_fragment, params)，统一用 lower(email) 做大小写无关匹配。
    """
    if _include_excluded(qs) or not LEADERBOARD_EXCLUDE_EMAILS:
        return "", []
    placeholders = ",".join("?" for _ in LEADERBOARD_EXCLUDE_EMAILS)
    return " AND lower(%semail) NOT IN (%s)" % (prefix, placeholders), sorted(LEADERBOARD_EXCLUDE_EMAILS)


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


def _trusted_keep_path(raw):
    """Feishu/attribution 写入的 effective_dept 是可信组织路径。

    旧 usage.dept 的裸团队别名不能自动挂到 Keep 树；但新 attribution 的
    effective_dept 已经过同步/回填确认，可以补上 Keep 根以便部门榜 roll-up。
    """
    if not raw:
        return None
    n = _normalize_dept_path(raw)
    if not n or str(n).lower() in ("unknown", "none"):
        return None
    if n.startswith("Keep"):
        return n
    if "/" in n:
        return "Keep/" + n
    return None


def _canonical_dept_key(raw_path):
    """Feishu/Feilian department path -> stable comparison key."""
    text = unicodedata.normalize("NFKC", _sstr(raw_path)).strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    parts = []
    for part in re.split(r"/+", text):
        part = re.sub(r"\s+", " ", part).strip()
        if part:
            parts.append(part)
    if parts and parts[0].lower() == "keep":
        parts = parts[1:]
    return "/".join(parts)


def _is_business_outsourcing_dept(raw_path):
    key = _canonical_dept_key(raw_path)
    parts = key.split("/") if key else []
    if "合作商" in parts:
        i = parts.index("合作商")
        if len(parts) > i + 1 and parts[i + 1] == "W":
            return True
    return bool(_SP_RE.search(_sstr(raw_path)))


def _active_attribution_map(conn):
    out = {}
    try:
        rows = conn.execute(
            "SELECT source_dept_key, target_dept_path, spend_bucket, rule, active"
            " FROM department_attributions"
            " WHERE COALESCE(target_dept_path,'')<>''"
            " AND (active=1 OR (active=0 AND (rule='chat_owner_department'"
            " OR spend_bucket='pending_business_outsourcing')))"
        ).fetchall()
    except Exception:
        return out
    for key, target, bucket, rule, active in rows:
        out.setdefault(key or "", []).append((target, bucket, rule, active))
    return out


def _attribution_signature(conn):
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(updated_at), ''),"
            " SUM(CASE WHEN active=1 THEN 1 ELSE 0 END)"
            " FROM department_attributions"
        ).fetchone()
    except Exception:
        return "missing"
    return "%s:%s:%s" % (row[0] or 0, row[1] or "", row[2] or 0)


def _usage_backfill_needed(conn):
    usage_cols = _table_columns(conn, "usage")
    required = {"raw_dept", "effective_dept", "spend_bucket", "attribution_source"}
    if not required.issubset(usage_cols):
        return True
    if _state_get(conn, "usage_backfill_required") == "1":
        return True
    current_sig = _attribution_signature(conn)
    if _state_get(conn, "usage_attribution_signature") != current_sig:
        return True
    if _state_get(conn, "usage_backfill_complete") == "1":
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM usage WHERE COALESCE(raw_dept,'')=''"
            " OR COALESCE(effective_dept,'')=''"
            " OR COALESCE(spend_bucket,'')='' LIMIT 1"
        ).fetchone()
    except Exception:
        return False
    return bool(row)


def _attribution_for_raw_dept(conn, raw_dept, active_map=None):
    raw_dept = _sstr(raw_dept)
    key = _canonical_dept_key(raw_dept)
    default_bucket = BUCKET_UNRESOLVED if _is_business_outsourcing_dept(raw_dept) else BUCKET_EMPLOYEE
    default = {
        "raw_dept": raw_dept,
        "effective_dept": raw_dept,
        "spend_bucket": default_bucket,
        "attribution_source": "unresolved" if default_bucket == BUCKET_UNRESOLVED else "direct",
    }
    if not key:
        return default
    if active_map is None:
        try:
            rows = conn.execute(
                "SELECT target_dept_path, spend_bucket, rule, active FROM department_attributions"
                " WHERE source_dept_key=? AND COALESCE(target_dept_path,'')<>''"
                " AND (active=1 OR (active=0 AND (rule='chat_owner_department'"
                " OR spend_bucket='pending_business_outsourcing')))",
                (key,),
            ).fetchall()
        except Exception:
            return default
    else:
        rows = active_map.get(key, [])
    if len(rows) != 1:
        return default
    target, bucket, rule = rows[0][:3]
    active = rows[0][3] if len(rows[0]) > 3 else 1
    if not active and (rule == "chat_owner_department" or bucket == BUCKET_PENDING_BUSINESS):
        bucket = BUCKET_PENDING_BUSINESS
    bucket = bucket or default_bucket
    if bucket not in (BUCKET_EMPLOYEE, BUCKET_BUSINESS, BUCKET_PENDING_BUSINESS, BUCKET_UNRESOLVED):
        bucket = default_bucket
    return {
        "raw_dept": raw_dept,
        "effective_dept": target or raw_dept,
        "spend_bucket": bucket,
        "attribution_source": rule or "department_attribution",
    }


def _backfill_usage_attribution(conn, dry_run=True):
    _ensure_usage_attribution_columns(conn)
    _ensure_app_state_table(conn)
    active_map = _active_attribution_map(conn)
    rows = conn.execute(
        "SELECT rowid, COALESCE(raw_dept,''), COALESCE(dept,''),"
        " COALESCE(effective_dept,''), COALESCE(spend_bucket,'') FROM usage"
    ).fetchall()
    summary = {
        "dry_run": bool(dry_run),
        "would_update": 0,
        "updated": 0,
        "employee_staff_outsourcing": 0,
        "business_outsourcing": 0,
        "pending_business_outsourcing": 0,
        "unresolved": 0,
    }
    for rowid, raw_dept, dept, effective_dept, spend_bucket in rows:
        raw = raw_dept or dept
        attr = _attribution_for_raw_dept(conn, raw, active_map)
        bucket = attr["spend_bucket"]
        if bucket not in summary:
            bucket = BUCKET_UNRESOLVED
            attr["spend_bucket"] = bucket
        summary[bucket] += 1
        needs = (
            raw_dept != attr["raw_dept"]
            or effective_dept != attr["effective_dept"]
            or dept != attr["effective_dept"]
            or spend_bucket != attr["spend_bucket"]
        )
        if not needs:
            continue
        summary["would_update"] += 1
        if dry_run:
            continue
        conn.execute(
            "UPDATE usage SET raw_dept=?, effective_dept=?, dept=?, spend_bucket=?,"
            " attribution_source=? WHERE rowid=?",
            (
                attr["raw_dept"],
                attr["effective_dept"],
                attr["effective_dept"],
                attr["spend_bucket"],
                attr["attribution_source"],
                rowid,
            ),
        )
        summary["updated"] += 1
    if not dry_run:
        _state_set(conn, "usage_backfill_required", "0")
        _state_set(conn, "usage_attribution_signature", _attribution_signature(conn))
        _state_set(conn, "usage_backfill_complete", "1")
    return summary


def _directory_identity_for_email(conn, email):
    if not email or "@" not in email:
        return None
    cols = _table_columns(conn, "people")
    if not {"source", "raw_dept", "effective_dept", "spend_bucket"}.issubset(cols):
        return None
    row = conn.execute(
        "SELECT name, avatar, dept, raw_dept, effective_dept, spend_bucket,"
        " attribution_source, source, status FROM people WHERE lower(email)=?",
        (email.lower(),),
    ).fetchone()
    if not row:
        return None
    source = row[7] or ""
    if source != "feishu":
        return None
    return {
        "name": row[0] or "",
        "avatar": row[1] or "",
        "dept": row[4] or row[2] or row[3] or "",
        "raw_dept": row[3] or row[2] or "",
        "effective_dept": row[4] or row[2] or row[3] or "",
        "spend_bucket": row[5] or BUCKET_EMPLOYEE,
        "attribution_source": row[6] or "feishu_directory",
        "source": source or "people",
        "status": row[8] or "active",
    }


def _report_identity(conn, serial, payload_email, serial_identity):
    ident = serial_identity or {}
    email = ident.get("email") or payload_email or ("sn:" + serial)
    email = _sstr(email)
    directory = _directory_identity_for_email(conn, email)
    if directory:
        return {
            "email": email,
            "name": directory.get("name") or email.split("@")[0],
            "avatar": directory.get("avatar") or "",
            "dept": directory.get("effective_dept") or directory.get("dept") or "unknown",
            "raw_dept": directory.get("raw_dept") or directory.get("dept") or "unknown",
            "effective_dept": directory.get("effective_dept") or directory.get("dept") or "unknown",
            "spend_bucket": directory.get("spend_bucket") or BUCKET_EMPLOYEE,
            "attribution_source": directory.get("attribution_source") or "feishu_directory",
            "source": "feishu",
            "preserve_people": True,
        }
    raw_dept = ident.get("department") or "unknown"
    attr = _attribution_for_raw_dept(conn, raw_dept)
    return {
        "email": email,
        "name": ident.get("name") or email.split("@")[0],
        "avatar": ident.get("avatar") or "",
        "dept": attr["effective_dept"] or raw_dept,
        "raw_dept": attr["raw_dept"] or raw_dept,
        "effective_dept": attr["effective_dept"] or raw_dept,
        "spend_bucket": attr["spend_bucket"],
        "attribution_source": attr["attribution_source"],
        "source": "feilian" if ident else "payload",
        "preserve_people": False,
    }


def _set_usage_attribution(conn, email, period_type, period, source, client, provider, model, identity):
    cols = _table_columns(conn, "usage")
    if not {"raw_dept", "effective_dept", "spend_bucket", "attribution_source"}.issubset(cols):
        return
    conn.execute(
        "UPDATE usage SET dept=?, raw_dept=?, effective_dept=?, spend_bucket=?, attribution_source=?"
        " WHERE email=? AND period_type=? AND period=? AND source=? AND client=?"
        " AND provider=? AND model=?",
        (
            identity.get("effective_dept") or identity.get("dept") or "",
            identity.get("raw_dept") or identity.get("dept") or "",
            identity.get("effective_dept") or identity.get("dept") or "",
            identity.get("spend_bucket") or BUCKET_EMPLOYEE,
            identity.get("attribution_source") or "",
            email, period_type, period, source, client, provider, model,
        ),
    )


def _upsert_report_person(conn, identity):
    email = identity.get("email") or ""
    if not email:
        return
    cols = _table_columns(conn, "people")
    if identity.get("preserve_people") and "source" in cols:
        existing = conn.execute("SELECT source FROM people WHERE email=?", (email,)).fetchone()
        if existing and existing[0] == "feishu":
            return
    name = identity.get("name") or email.split("@")[0]
    avatar = identity.get("avatar") or ""
    dept = identity.get("effective_dept") or identity.get("dept") or "unknown"
    if {"raw_dept", "effective_dept", "spend_bucket", "source"}.issubset(cols):
        conn.execute(
            "INSERT OR REPLACE INTO people(email, name, avatar, dept, raw_dept, effective_dept,"
            " spend_bucket, attribution_source, source, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                email, name, avatar, dept,
                identity.get("raw_dept") or dept,
                identity.get("effective_dept") or dept,
                identity.get("spend_bucket") or BUCKET_EMPLOYEE,
                identity.get("attribution_source") or "",
                identity.get("source") or "",
                datetime.datetime.now().isoformat(timespec="seconds"),
            ),
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO people(email, name, avatar, dept) VALUES(?,?,?,?)",
            (email, name, avatar, dept))


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
def _personal_board_rows(conn, qs, auth_user=None):
    """个人榜「按人」计算 —— 唯一口径, /v1/leaderboard 与 /v1/ai/usage 共用(2026-06-16)。

    token = SUM(total) + 飞书权益点(1点=1token);
    cost  = 公司实付 = 网关实销 SUM(CASE source IN('litellm','api') THEN cost)
                      + 飞书点成本(credits×USD_PER_POINT)
                      + 订阅费按「窗口∩席位区间」摊销(_interval_fraction)。
    过滤 litellm_agent + 合成身份 + 离职(默认)。已并入飞书(含纯飞书行)并按 tokens 降序。
    不接受 ?client(那是工具榜, 仍在 _leaderboard 内)。
    """
    where, params = _range_clause(qs, "u.")
    sd = _show_departed(qs)
    dep_clause = _departed_filter(sd, "u.")
    ex_clause, ex_params = _excluded_filter(qs, "u.")
    departed = _departed_set(conn)
    cost_start, cost_end = _cost_window(qs)
    today = datetime.date.today()
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
    """ % (where, dep_clause, ex_clause), params + ex_params).fetchall()
    comp = {}
    for cr in conn.execute("""
        SELECT u.email, u.client, SUM(u.total)
        FROM usage u
        WHERE %s AND u.source != 'litellm_agent'
          AND u.email NOT LIKE 'litellm-key:%%' AND u.email NOT LIKE 'litellm-user:%%'%s%s
        GROUP BY u.email, u.client
    """ % (where, dep_clause, ex_clause), params + ex_params).fetchall():
        comp.setdefault(cr[0], []).append({"client": cr[1], "tokens": cr[2] or 0})
    result = []
    by_email = {}
    for r in rows:
        row = {
            "email": r[0], "dept": _to_keep(r[13]) or _normalize_dept_path(r[1]),
            "input": r[2] or 0, "output": r[3] or 0,
            "cache_read": r[4] or 0, "cache_write": r[5] or 0,
            "reasoning": r[6] or 0, "tokens": r[7] or 0,
            "cost": round(r[8] or 0, 4), "messages": r[9] or 0,
            "name": r[10] or (r[0] or "").split("@")[0],
            "avatar": r[11] or "",
            "via": r[12] or "",
            "departed": r[0] in departed,
            "composition": list(comp.get(r[0], [])),
            "subs": [],
        }
        result.append(row)
        by_email[r[0]] = row

    subs_by_email = load_subscriptions(conn)

    # 飞书 AI 权益并入(1 点 = 1 token; 纯飞书用户也建行)。
    frng, fparams = _feishu_range(qs)
    fdep = "" if sd else " AND email NOT IN (SELECT email FROM departed)"
    fex, fex_params = _excluded_filter(qs, "")
    for fr in conn.execute(
            "SELECT email, MAX(name), MAX(dept), MAX(avatar), SUM(credits)"
            " FROM feishu_member WHERE 1=1%s%s%s"
            " GROUP BY email HAVING SUM(credits)>0" % (frng, fdep, fex),
            fparams + fex_params).fetchall():
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
        row["feishu_credits"] = credits
        fc = round(credits * FEISHU_USD_PER_POINT, 4)
        row["feishu_cost"] = fc
        row["cost"] = round(float(row["cost"] or 0) + credits * FEISHU_USD_PER_POINT, 4)
        row["composition"].append({"client": "飞书AI权益", "tokens": credits})

    # 订阅费按窗口摊销并入个人榜 cost。
    usage_window_start = {}
    if cost_start is None:
        for mr in conn.execute("""
            SELECT u.email, MIN(u.period)
            FROM usage u
            WHERE u.period_type='day' AND u.source != 'litellm_agent'
            GROUP BY u.email
        """).fetchall():
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

    # email 本应大小写不敏感: 把同一人(lower(email) 相同)的大小写变体行合并成一行,
    # 否则同一人会被拆成多行(token/cost 分裂)。生产 0 例(防御性; 也让 /v1/ai/usage 单人聚合
    # 与本榜分毫一致, 2026-06-16 评审)。
    merged = {}
    for row in result:
        k = (row["email"] or "").lower()
        m = merged.get(k)
        if m is None:
            merged[k] = row
            continue
        m["tokens"] = (m["tokens"] or 0) + (row["tokens"] or 0)
        m["cost"] = round(float(m["cost"] or 0) + float(row["cost"] or 0), 4)
        m["messages"] = (m.get("messages") or 0) + (row.get("messages") or 0)
        m["composition"].extend(row["composition"])
        if row.get("feishu_credits"):
            m["feishu_credits"] = (m.get("feishu_credits") or 0) + row["feishu_credits"]
    result = filter_person_rows_for_auth(list(merged.values()), auth_user)

    for row in result:
        tot = row["tokens"] or 0
        for x in row["composition"]:
            x["pct"] = round(x["tokens"] / tot * 100, 1) if tot else 0
        row["composition"].sort(key=lambda x: x["tokens"], reverse=True)
    result.sort(key=lambda x: x["tokens"] or 0, reverse=True)
    return result


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

    # ---- session auth (Feishu OAuth) --------------------------------------
    def _parse_cookies(self):
        out = {}
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _session_user(self, conn):
        """Return _user_roles dict for the cookie session, or None."""
        sid = self._parse_cookies().get(SESSION_COOKIE)
        email = session_email(conn, sid)
        if not email:
            return None
        return _user_roles(conn, email)

    def _scope_rows(self, rows):
        """Filter per-person / per-dept row dicts to the caller's visible scope.

        No-op when no scope user is set (admin, or shadow mode). Matches each
        row by its email + effective_dept/dept through email_in_scope().
        """
        user = getattr(self, "_scope_user", None)
        if not user or not isinstance(rows, list):
            return rows
        return [r for r in rows
                if isinstance(r, dict) and email_in_scope(
                    user, r.get("email", ""),
                    r.get("effective_dept") or r.get("dept", ""))]

    def _set_scope_arg(self, auth_user):
        if auth_user is not None:
            self._scope_user = auth_user
        return getattr(self, "_scope_user", None)

    def _cookie_header(self, sid):
        parts = ["%s=%s" % (SESSION_COOKIE, sid), "Path=/", "HttpOnly",
                 "SameSite=Lax", "Max-Age=%d" % SESSION_TTL]
        if AUTH_COOKIE_SECURE:
            parts.append("Secure")
        return "; ".join(parts)

    def _send_redirect(self, location, set_cookie=None, clear_cookie=False):
        self.send_response(302)
        self.send_header("Location", location)
        if set_cookie is not None:
            self.send_header("Set-Cookie", self._cookie_header(set_cookie))
        if clear_cookie:
            self.send_header(
                "Set-Cookie",
                "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % SESSION_COOKIE)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _auth_login(self, conn, qs):
        if not FEISHU_APP_ID or not FEISHU_OAUTH_REDIRECT_URI:
            return self._send(500, {"error": "feishu oauth not configured"})
        nxt = _safe_next_path((qs.get("next") or ["/"])[0])
        state = create_oauth_state(conn, nxt)
        return self._send_redirect(feishu_authorize_url(state))

    def _auth_callback(self, conn, qs):
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]
        if not code or not state:
            return self._send(400, {"error": "missing code or state"})
        redirect = consume_oauth_state(conn, state)
        if redirect is None:
            return self._send(400, {"error": "invalid or expired state"})
        try:
            info = feishu_exchange_code(code)
        except Exception as e:  # noqa: BLE001 - surface upstream failure, never 200
            return self._send(502, {"error": "feishu code exchange failed",
                                    "detail": str(e)[:200]})
        email = (info.get("email") or "").strip().lower()
        if not email:
            # No open_id-only / empty-email sessions — hard 403 per security model.
            return self._send(403, {"error": "feishu profile has no email; access denied"})
        sid = create_session(conn, email)
        return self._send_redirect(redirect or "/", set_cookie=sid)

    def _auth_logout(self, conn):
        delete_session(conn, self._parse_cookies().get(SESSION_COOKIE))
        return self._send_redirect("/", clear_cookie=True)

    def _me(self, conn):
        user = self._session_user(conn)
        if not user:
            return self._send(401, {"error": "not authenticated"})
        name, dept = "", ""
        try:
            row = conn.execute(
                "SELECT COALESCE(name,''), COALESCE(effective_dept, dept, '')"
                " FROM people WHERE lower(email)=?", (user["email"],)).fetchone()
            if row:
                name, dept = row[0], row[1]
        except Exception:
            pass
        return self._send(200, {
            "email": user["email"], "name": name, "dept": dept,
            "roles": user["roles"], "scope": user["scope"],
            "is_admin": user["is_admin"],
            "owned_departments": user["owned_departments"],
            "auth_enforced": auth_enforced(),
        })

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

    def _dashboard_or_login(self, path, query=""):
        if not auth_enforced():
            return self._dashboard()
        conn = db()
        try:
            if self._session_user(conn):
                return self._dashboard()
        finally:
            conn.close()
        nxt = path + (("?" + query) if query else "")
        return self._send_redirect("/v1/auth/login?" + _urlencode({"next": nxt or "/"}))

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
        serial = _clean_serial(p.get("serial", ""))
        lifetime_entries = (p.get("models") or {}).get("entries") or []
        monthly_entries = (p.get("monthly") or {}).get("entries") or []

        # 上报来源:仅接受 mdm / manual,其它一律按 mdm(老客户端不带 via 时也是 mdm)
        via = p.get("via") if p.get("via") in ("mdm", "manual") else "mdm"

        # 服务端用序列号经飞连反解身份（机器侧零配置），但一旦能拿到 email，
        # 飞书 people 目录是人和组织架构真源；飞连只做设备归属兜底。
        ident = _resolve_serial(serial)
        conn = db()
        identity = _report_identity(conn, serial, p.get("email"), ident)
        email = identity["email"]
        dept = identity["effective_dept"] or identity["dept"] or "unknown"
        up_lt = _upsert_lifetime(conn, email, dept, lifetime_entries, identity)
        up_mo = _upsert_monthly(conn, email, dept, monthly_entries, identity)
        up_dy = _upsert_daily(conn, email, dept, p.get("graph") or {}, identity)
        # 落人员档案:中文姓名 + 飞连头像 + 完整部门路径（看板 join 用）
        _upsert_report_person(conn, identity)
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
            return self._dashboard_or_login(path, parsed.query)
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
            # Authentication routes — always available, independent of AUTH_ENFORCE.
            if path == "/v1/auth/login":
                return self._auth_login(conn, qs)
            if path == "/v1/auth/callback":
                return self._auth_callback(conn, qs)
            if path == "/v1/auth/logout":
                return self._auth_logout(conn)
            if path == "/v1/me":
                return self._me(conn)

            # Authorization gate — only does work when enforcing (shadow=no-op).
            self._scope_user = None
            if auth_enforced() and path in DATA_ROUTES:
                user = self._session_user(conn)
                decision = authorize_request(user, path, True)
                if decision == "401":
                    return self._send(401, {"error": "login required",
                                            "login": "/v1/auth/login"})
                if decision == "403":
                    return self._send(403, {"error": "forbidden for your role"})
                if user and not user.get("is_admin"):
                    # Row-scoped route: filter rows to the caller + strip admin flags.
                    self._scope_user = user
                    qs.pop("include_excluded", None)
                    qs.pop("show_departed", None)

            if path == "/v1/leaderboard":
                return self._leaderboard(conn, qs)
            if path == "/v1/agent_leaderboard":
                return self._agent_leaderboard(conn, qs)
            if path == "/v1/agent_owner_summary":
                return self._agent_owner_summary(conn, qs)
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

    def _feishu(self, conn, qs, auth_user=None):
        """飞书 AI 权益(独立板块,单位=点)。按天聚合:额度盘 + 全员逐人榜 + 部门榜 + 趋势。
        区间同 token 榜:?from=&to= 或 ?days=N(usage_date 上过滤);默认近 30 天(=回填窗口)。
        ?show_departed=1 才纳入离职。"""
        if auth_user is not None:
            self._scope_user = auth_user
        scope_user = getattr(self, "_scope_user", None)
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
        sd = _show_departed(qs)
        dep = "" if sd else " AND f.email NOT IN (SELECT email FROM departed)"
        ex, ex_params = _excluded_filter(qs, "f.")
        member_rows = conn.execute(
            "SELECT f.email, MAX(f.name),"
            " COALESCE(MAX(NULLIF(p.effective_dept,'')), MAX(NULLIF(p.dept,'')), MAX(f.dept)),"
            " MAX(f.avatar), SUM(f.credits),"
            " SUM(CASE WHEN f.feature_key='AI_credits' THEN f.credits ELSE 0 END),"
            " SUM(CASE WHEN f.feature_key='aily_credits' THEN f.credits ELSE 0 END),"
            " MIN(f.usage_date), MAX(f.usage_date)"
            " FROM feishu_member f LEFT JOIN people p ON lower(p.email)=lower(f.email)"
            " WHERE 1=1%s%s%s"
            " GROUP BY f.email HAVING SUM(f.credits)>0 ORDER BY SUM(f.credits) DESC"
            % (rng, dep, ex),
            params + ex_params).fetchall()
        if scope_user and not scope_user.get("is_admin"):
            member_rows = [
                r for r in member_rows
                if email_in_scope(scope_user, r[0], _scope_dept(r[0], r[2] or ""))
            ]
        if member_rows:
            ps = min((r[7] for r in member_rows if r[7]), default=None)
            pe = max((r[8] for r in member_rows if r[8]), default=None)
        else:
            ps = pe = None
        is_admin_scope = not scope_user or scope_user.get("is_admin")
        quota = []
        trend = []
        if is_admin_scope:
            quota = [{"feature_key": r[0], "quota": r[1] or 0,
                      "used": r[2] or 0, "remain": r[3] or 0}
                     for r in conn.execute(
                         "SELECT feature_key,quota,used,remain FROM feishu_quota WHERE period_start="
                         "(SELECT max(period_start) FROM feishu_quota) ORDER BY quota DESC").fetchall()]
            trend = [{"usage_date": r[0], "biz_type": r[1],
                      "credits": r[2] or 0, "user_count": r[3] or 0}
                     for r in conn.execute(
                         "SELECT usage_date,biz_type,credits,user_count FROM feishu_trend"
                         " ORDER BY usage_date").fetchall()]
        members = [{"email": r[0], "name": r[1] or (r[0] or "").split("@")[0],
                    "dept": r[2] or "unknown", "avatar": r[3] or "",
                    "credits": r[4] or 0, "ai_credits": r[5] or 0,
                    "aily_credits": r[6] or 0}
                   for r in member_rows]
        dept_map = {}
        for r in member_rows:
            d = r[2] or "unknown"
            cur = dept_map.setdefault(d, {"dept": d, "credits": 0, "people": set()})
            cur["credits"] += r[4] or 0
            if r[0]:
                cur["people"].add(r[0])
        dept = sorted(
            ({"dept": v["dept"], "credits": v["credits"], "people": len(v["people"])}
             for v in dept_map.values()),
            key=lambda x: x["credits"],
            reverse=True,
        )
        payload = {"period_start": ps, "period_end": pe,
                   "quota": quota, "members": members, "dept": dept, "trend": trend}
        payload.update(billing)
        self._send(200, payload)


    def _leaderboard(self, conn, qs, auth_user=None):
        """按人聚合(区间 ?days=N 或全部),join people 取中文姓名+头像+完整部门路径。
        同一人当天 Cursor+Claude+Codex 的 token 自动求和(GROUP BY email)。
        无 ?client → 个人榜走共用 _personal_board_rows(与 /v1/ai/usage 同口径)。"""
        if auth_user is not None:
            self._scope_user = auth_user
        cli = (qs.get("client") or [None])[0]
        if not cli:
            return self._send(200, {"leaderboard": filter_person_rows_for_auth(
                _personal_board_rows(conn, qs), getattr(self, "_scope_user", None))})
        where, params = _range_clause(qs, "u.")
        sd = _show_departed(qs)
        dep_clause = _departed_filter(sd, "u.")
        ex_clause, ex_params = _excluded_filter(qs, "u.")
        departed = _departed_set(conn)
        cost_start, cost_end = _cost_window(qs)
        today = datetime.date.today()
        # 可选 ?client=Claude Code|Codex CLI|... → 只统计该工具(Claude 榜 / Codex 榜复用此端点)
        cli = (qs.get("client") or [None])[0]
        # client 匹配大小写不敏感:历史上 Hermes 有上报端写过小写 'hermes',
        # 精确匹配会把这些行漏出榜单与推断;归一(ingest 已做)之前的存量也要能查到。
        cli_clause = " AND lower(u.client) = ?" if cli else ""
        params2 = list(params) + ex_params + ([cli.lower()] if cli else [])
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
              AND u.email NOT LIKE 'litellm-key:%%' AND u.email NOT LIKE 'litellm-user:%%'%s%s%s
            GROUP BY u.email
            HAVING SUM(u.total) > 0
            ORDER BY SUM(u.total) DESC
        """ % (where, dep_clause, ex_clause, cli_clause), params2).fetchall()
        # 每人按工具(client)的构成:Claude/Codex/Cursor/Gemini/... 占比
        comp = {}
        for cr in conn.execute("""
            SELECT u.email, u.client, SUM(u.total)
            FROM usage u
            WHERE %s AND u.source != 'litellm_agent'
              AND u.email NOT LIKE 'litellm-key:%%' AND u.email NOT LIKE 'litellm-user:%%'%s%s%s
            GROUP BY u.email, u.client
        """ % (where, dep_clause, ex_clause, cli_clause), params2).fetchall():
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
                WHERE %s AND u.source != 'litellm_agent'%s%s%s
                GROUP BY u.email, u.model
            """ % (where, dep_clause, ex_clause, cli_clause), params2).fetchall():
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

        # (个人榜的飞书并入 + 订阅摊销已移到 _personal_board_rows; 此处只剩工具榜 cli 路径)
        # 飞书并入后总量可能变 → 统一算 composition 占比 + 重排
        for row in result:
            tot = row["tokens"] or 0
            for x in row["composition"]:
                x["pct"] = round(x["tokens"] / tot * 100, 1) if tot else 0
            row["composition"].sort(key=lambda x: x["tokens"], reverse=True)
        result.sort(key=lambda x: x["tokens"] or 0, reverse=True)
        self._send(200, {"leaderboard": filter_person_rows_for_auth(
            result, getattr(self, "_scope_user", None))})

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

    def _agent_owner_summary(self, conn, qs):
        """按归属人聚合 Agent 消耗,用于解释“个人榜不含 Agent,但可看归属 Agent 合计”。

        people 行中 agent:<alias> 的 dept 字段存 owner 中文名,沿用 Agent 榜现有归属模型。
        可选 ?owner=<中文名> 过滤单个 owner。
        """
        where, params = _range_clause(qs, "u.")
        owner = (qs.get("owner") or [None])[0]
        owner_clause = " AND COALESCE(p.dept, '') = ?" if owner else ""
        if owner:
            params = list(params) + [owner]
        rows = conn.execute("""
            SELECT COALESCE(p.dept, '') AS owner,
                   COUNT(DISTINCT u.email),
                   SUM(u.total), SUM(u.cost), SUM(u.messages)
            FROM usage u LEFT JOIN people p ON p.email = u.email
            WHERE %s AND u.source = 'litellm_agent'%s
            GROUP BY COALESCE(p.dept, '')
            HAVING SUM(u.total) > 0
            ORDER BY SUM(u.total) DESC
        """ % (where, owner_clause), params).fetchall()
        result = [{
            "owner": r[0] or "",
            "agents": r[1] or 0,
            "tokens": r[2] or 0,
            "cost": round(r[3] or 0, 4),
            "messages": r[4] or 0,
        } for r in rows]
        self._send(200, {"agent_owner_summary": result})

    def _cursor(self, conn, qs):
        """Cursor 维度榜:按 token 排(与个人/工具/模型榜口径统一),带 token 明细 +
        花费($)/请求数 + 中文姓名/头像/部门。token 来自 Cursor Admin API 的
        filtered-usage-events.tokenUsage(真 token,见 cursor_sync.py)。支持全局区间。"""
        where, params = _range_clause(qs, "u.")
        sd = _show_departed(qs)
        dep_clause = _departed_filter(sd, "u.")
        ex_clause, ex_params = _excluded_filter(qs, "u.")
        departed = _departed_set(conn)
        rows = conn.execute("""
            SELECT u.email, MAX(u.dept),
                   SUM(u.input), SUM(u.output), SUM(u.cache_read), SUM(u.cache_write),
                   SUM(u.reasoning), SUM(u.total), SUM(u.cost), SUM(u.messages),
                   MAX(p.name), MAX(p.avatar), MAX(p.dept)
            FROM usage u LEFT JOIN people p ON p.email = u.email
            WHERE %s AND u.source='cursor'%s%s
            GROUP BY u.email
            ORDER BY SUM(u.total) DESC
        """ % (where, dep_clause, ex_clause), params + ex_params).fetchall()
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
        self._send(200, {"cursor": filter_person_rows_for_auth(
            result, getattr(self, "_scope_user", None))})

    def _teams(self, conn, qs, auth_user=None):
        """按部门(team)聚合(区间或全部)。dept 完整路径,含使用人数(people)+部门总人数
        (headcount,来自飞连)+活跃率(active_rate=people/headcount*100)。跨工具求和。
        默认剔除离职用户(token 与人数都不计);?show_departed=1 时纳入。"""
        if auth_user is not None:
            self._scope_user = auth_user
        scope_user = getattr(self, "_scope_user", None)
        where, params = _range_clause(qs)
        sd = _show_departed(qs)
        dep_clause = _departed_filter(sd, "")
        ex_clause, ex_params = _excluded_filter(qs, "")
        # 取 email 级明细。新 attribution schema 存在时,以 effective_dept + spend_bucket
        # 作为部门榜口径；旧 schema 不存在这些列时保持原有 usage.dept 行为。
        usage_cols = _table_columns(conn, "usage")
        has_attr = "effective_dept" in usage_cols and "spend_bucket" in usage_cols
        if has_attr:
            rows = conn.execute("""
                SELECT email,
                       COALESCE(NULLIF(effective_dept,''), dept) AS effective_dept,
                       COALESCE(NULLIF(raw_dept,''), dept) AS raw_dept,
                       COALESCE(NULLIF(spend_bucket,''), ?) AS spend_bucket,
                       SUM(total), SUM(cost), SUM(messages)
                FROM usage
                WHERE %s AND source != 'litellm_agent'%s%s
                GROUP BY 1,2,3,4
            """ % (where, dep_clause, ex_clause),
                [BUCKET_EMPLOYEE] + params + ex_params).fetchall()
        else:
            rows = [
                (email, dept, dept, BUCKET_EMPLOYEE, tok, cost, msg)
                for email, dept, tok, cost, msg in conn.execute("""
                    SELECT email, dept, SUM(total), SUM(cost), SUM(messages)
                    FROM usage
                    WHERE %s AND source != 'litellm_agent'%s%s
                    GROUP BY email, dept
                """ % (where, dep_clause, ex_clause), params + ex_params).fetchall()
            ]

        # 用 people.effective_dept(飞书归因后的真实部门)把每个人归一到唯一的真实组织部门，
        # 老库没有 effective_dept 时回退到 people.dept。
        # 再把此人所有来源的用量收进该部门 → 单一 Keep 树，杜绝裸别名裂树。
        people_cols = _table_columns(conn, "people")
        if "effective_dept" in people_cols:
            pdept = dict(conn.execute(
                "SELECT email, COALESCE(NULLIF(effective_dept,''), dept) FROM people"
            ).fetchall())
        else:
            pdept = dict(conn.execute("SELECT email, dept FROM people").fetchall())
        if scope_user and not scope_user.get("is_admin"):
            rows = [
                r for r in rows
                if email_in_scope(scope_user, r[0], _scope_dept(r[0], r[2] or r[1], pdept, r[1]))
            ]
        if "spend_bucket" in people_cols:
            pbucket = dict(conn.execute(
                "SELECT email, COALESCE(NULLIF(spend_bucket,''), ?) FROM people",
                (BUCKET_EMPLOYEE,),
            ).fetchall())
        else:
            pbucket = {}

        per = {}  # (email, effective_dept, spend_bucket) -> {tok, cost, msg, depts:[...]}
        for email, dept, raw_dept, bucket, tok, cost, msg in rows:
            bucket = bucket or BUCKET_EMPLOYEE
            if bucket not in (BUCKET_EMPLOYEE, BUCKET_BUSINESS, BUCKET_PENDING_BUSINESS, BUCKET_UNRESOLVED):
                bucket = BUCKET_UNRESOLVED
            pkey = (email, dept or "", bucket)
            p = per.get(pkey)
            if p is None:
                p = {"tok": 0, "cost": 0.0, "msg": 0, "depts": [], "bucket": bucket,
                     "effective_dept": dept or ""}
                per[pkey] = p
            p["tok"] += tok or 0
            p["cost"] += cost or 0
            p["msg"] += msg or 0
            if dept:
                p["depts"].append(dept)
            if raw_dept and raw_dept != dept:
                p["depts"].append(raw_dept)

        def _canon_dept(email, depts, effective_dept=""):
            """每人规范部门：people.dept 优先 → usage 里最具体的可归一 Keep 路径 →
            都归不到则 'Keep/未归类'。统一过 _to_keep：外包折回真实部门、裸供应商(SP码)收口外部合作商，
            与 headcount 同口径。裸非 SP 组名/unknown 归不到 Keep → 未归类。"""
            if has_attr:
                trusted = _trusted_keep_path(effective_dept)
                if trusted:
                    return trusted
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

        def _bucket_metric():
            return {"tokens": 0, "cost": 0.0, "messages": 0, "credits": 0.0,
                    "token_users": set(), "aily_users": set()}

        def _node(path):
            n = nodes.get(path)
            if n is None:
                # token_users/aily_users 分开:人均按各自口径,活跃渗透取并集
                n = {"tokens": 0, "cost": 0.0, "messages": 0, "credits": 0.0,
                     "token_users": set(), "aily_users": set(),
                     "buckets": {
                         BUCKET_EMPLOYEE: _bucket_metric(),
                         BUCKET_BUSINESS: _bucket_metric(),
                         BUCKET_PENDING_BUSINESS: _bucket_metric(),
                         BUCKET_UNRESOLVED: _bucket_metric(),
                     }}
                nodes[path] = n
            return n

        nodes = {}  # path -> {tokens, cost, messages, credits, token_users, aily_users}
        for (email, _edept, _bucket), p in per.items():
            bucket = p.get("bucket") or BUCKET_EMPLOYEE
            cd = _canon_dept(email, p["depts"], p.get("effective_dept"))
            if cd == "Keep/未归类":
                continue   # 解析不到真实部门(离职/飞连外)→ 跳过,不污染部门榜(孙可 2026-06-11)
            for anc in _ancestors(cd):
                n = _node(anc)
                if bucket in VISIBLE_SPEND_BUCKETS:
                    n["tokens"] += p["tok"]
                    n["cost"] += p["cost"]
                    n["messages"] += p["msg"]
                    n["token_users"].add(email)
                if bucket in n["buckets"]:
                    b = n["buckets"][bucket]
                    b["tokens"] += p["tok"]
                    b["cost"] += p["cost"]
                    b["messages"] += p["msg"]
                    b["token_users"].add(email)

        # aily(飞书 AI 权益)并入部门榜:按天聚合,跟随所选区间(与 token 同窗口,默认近30天),
        # 不再取「最新月快照」死数据(孙可 2026-06-12:月累计污染部门榜)。单位「点」credits,
        # 不与 token 加总;aily 的人并进活跃集 → 活跃渗透取并集。
        frng, fparams = _feishu_range(qs)
        fdep = "" if sd else " AND email NOT IN (SELECT email FROM departed)"
        fex, fex_params = _excluded_filter(qs, "")
        aily_rows = conn.execute(
            "SELECT email, MAX(dept), SUM(credits) FROM feishu_member"
            " WHERE 1=1%s%s%s GROUP BY email HAVING SUM(credits)>0" % (frng, fdep, fex),
            fparams + fex_params).fetchall()
        if scope_user and not scope_user.get("is_admin"):
            aily_rows = [
                r for r in aily_rows
                if email_in_scope(scope_user, r[0], _scope_dept(r[0], r[1], pdept, ""))
            ]
        for email, fdept, credits in aily_rows:
            # people.dept 优先,否则用 feishu_member.dept;统一过 _to_keep —— 裸供应商(SP码)
            # 也收口到外部合作商,不再因「不以 Keep 开头」误落未归类(codex 评审发现)。
            cd = _to_keep(pdept.get(email)) or _to_keep(fdept)
            if not cd:
                continue   # 飞连查不到真实部门(离职/飞连外纯飞书用户)→ 跳过,不进未归类(孙可 2026-06-11)
            bucket = pbucket.get(email) or BUCKET_EMPLOYEE
            if bucket not in (BUCKET_EMPLOYEE, BUCKET_BUSINESS, BUCKET_PENDING_BUSINESS, BUCKET_UNRESOLVED):
                bucket = BUCKET_EMPLOYEE
            for anc in _ancestors(cd):
                n = _node(anc)
                n["aily_users"].add(email)
                if bucket in VISIBLE_SPEND_BUCKETS:
                    n["credits"] += credits or 0
                b = n["buckets"][bucket]
                b["credits"] += credits or 0
                b["aily_users"].add(email)

        def _view_metric(n, bucket=None):
            if bucket is None:
                token_users = n["token_users"]
                aily_users = n["aily_users"]
                return {
                    "tokens": n["tokens"],
                    "cost": round(n["cost"], 4),
                    "messages": n["messages"],
                    "credits": round(n["credits"], 2),
                    "people": len(token_users | aily_users),
                    "token_people": len(token_users),
                    "aily_people": len(aily_users),
                }
            b = n["buckets"].get(bucket) or _bucket_metric()
            return {
                "tokens": b["tokens"],
                "cost": round(b["cost"], 4),
                "messages": b["messages"],
                "credits": round(b["credits"], 2),
                "people": len(b["token_users"] | b["aily_users"]),
                "token_people": len(b["token_users"]),
                "aily_people": len(b["aily_users"]),
            }

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
                "department_full": _view_metric(n),
                "employee_staff_outsourcing": _view_metric(n, BUCKET_EMPLOYEE),
                "business_outsourcing": _view_metric(n, BUCKET_BUSINESS),
                "pending_business_outsourcing": _view_metric(n, BUCKET_PENDING_BUSINESS),
                "unresolved": _view_metric(n, BUCKET_UNRESOLVED),
            })
        result.sort(key=lambda x: -x["tokens"])
        self._send(200, {"teams": result})

    def _breakdown(self, conn, qs, auth_user=None):
        """四种维度聚合 lifetime 快照。
        by=client                  → 按 client 聚合
        by=client_model            → 按 client + model 聚合
        by=client_provider_model   → 按 client + provider + model 聚合（默认）
        """
        if auth_user is not None:
            self._scope_user = auth_user
        scope_user = getattr(self, "_scope_user", None)
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
        ex_clause, ex_params = _excluded_filter(qs, "")
        if scope_user and not scope_user.get("is_admin"):
            usage_cols = _table_columns(conn, "usage")
            dept_expr = "COALESCE(NULLIF(effective_dept,''), dept)" \
                if "effective_dept" in usage_cols else "dept"
            scoped_rows = conn.execute(
                """
                SELECT email, {dept_expr}, client, provider, model,
                       SUM(input), SUM(output), SUM(cache_read), SUM(cache_write),
                       SUM(reasoning), SUM(total), SUM(cost), SUM(messages)
                FROM usage
                WHERE {where} AND source != 'litellm_agent'{ex}
                GROUP BY email, {dept_expr}, client, provider, model
                """.format(dept_expr=dept_expr, where=where, ex=ex_clause),
                params + ex_params,
            ).fetchall()
            pdept = dict(conn.execute("SELECT email, dept FROM people").fetchall())
            agg = {}
            for r in scoped_rows:
                email, dept, client, provider, model = r[:5]
                if not email_in_scope(scope_user, email, _scope_dept(email, dept, pdept, dept)):
                    continue
                if by == "client":
                    key = (client, "", "")
                elif by == "model":
                    key = ("", "", model)
                elif by == "client_model":
                    key = (client, "", model)
                else:
                    key = (client, provider, model)
                cur = agg.setdefault(key, [0, 0, 0, 0, 0, 0, 0.0, 0])
                for i, val in enumerate(r[5:13]):
                    cur[i] += val or 0
            result = []
            for key, sums in sorted(agg.items(), key=lambda item: -item[1][5]):
                result.append({
                    "client": key[0], "provider": key[1], "model": key[2],
                    "input": sums[0], "output": sums[1],
                    "cache_read": sums[2], "cache_write": sums[3],
                    "reasoning": sums[4], "tokens": sums[5],
                    "cost": round(sums[6], 4), "messages": sums[7],
                })
            return self._send(200, {"by": by, "breakdown": result})

        sql = (
            "SELECT {extra}, "
            "SUM(input), SUM(output), SUM(cache_read), SUM(cache_write), "
            "SUM(reasoning), SUM(total), SUM(cost), SUM(messages) "
            "FROM usage WHERE {where} AND source != 'litellm_agent'{ex} "
            "GROUP BY {grp} ORDER BY SUM(total) DESC"
        ).format(extra=select_extra, where=where, ex=ex_clause, grp=group_cols)

        rows = conn.execute(sql, params + ex_params).fetchall()
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
        su = getattr(self, "_scope_user", None)
        filter_qs = dict(qs or {})
        if su and not su.get("is_admin"):
            filter_qs.pop("include_excluded", None)
            filter_qs.pop("show_departed", None)
        ex_clause, ex_params = _excluded_filter(filter_qs, "")
        departed_clause = _departed_filter(_show_departed(filter_qs), "")
        scoped_department_default = False
        if su:
            # Member no-filter collapses to self. Department owners default to
            # their owned subtree. Explicit ?email must be inside scope.
            if not email_filter:
                if su.get("scope") == "department" and su.get("owned_departments"):
                    scoped_department_default = True
                else:
                    email_filter = su["email"]
            else:
                _r = conn.execute(
                    "SELECT COALESCE(effective_dept, dept, '') FROM people"
                    " WHERE lower(email)=?", ((email_filter or "").lower(),)).fetchone()
                if not email_in_scope(su, email_filter, _r[0] if _r else ""):
                    return self._send(403, {"error": "forbidden for your role"})
        if scoped_department_default:
            usage_cols = _table_columns(conn, "usage")
            dept_expr = "COALESCE(NULLIF(effective_dept,''), dept)" \
                if "effective_dept" in usage_cols else "dept"
            people_cols = _table_columns(conn, "people")
            if "effective_dept" in people_cols:
                pdept = dict(conn.execute(
                    "SELECT email, COALESCE(NULLIF(effective_dept,''), dept) FROM people"
                ).fetchall())
            else:
                pdept = dict(conn.execute("SELECT email, dept FROM people").fetchall())
            scoped_rows = conn.execute("""
                SELECT email, {dept_expr}, period, SUM(input), SUM(output),
                       SUM(cache_read), SUM(cache_write), SUM(reasoning),
                       SUM(total), SUM(cost), SUM(messages)
                FROM usage
                WHERE period_type='month'{ex}{dep}
                GROUP BY email, {dept_expr}, period
            """.format(dept_expr=dept_expr, ex=ex_clause, dep=departed_clause),
                ex_params).fetchall()
            agg = {}
            for email, dept, period, *vals in scoped_rows:
                if not email_in_scope(su, email, _scope_dept(email, dept, pdept, dept)):
                    continue
                cur = agg.setdefault(period, [0, 0, 0, 0, 0, 0, 0.0, 0])
                for i, val in enumerate(vals):
                    cur[i] += val or 0
            rows = [(period,) + tuple(vals) for period, vals in sorted(agg.items())]
        elif email_filter:
            rows = conn.execute("""
                SELECT period, SUM(input), SUM(output), SUM(cache_read),
                       SUM(cache_write), SUM(reasoning), SUM(total), SUM(cost), SUM(messages)
                FROM usage
                WHERE period_type='month' AND email=?%s%s
                GROUP BY period ORDER BY period
            """ % (ex_clause, departed_clause), [email_filter] + ex_params).fetchall()
        else:
            rows = conn.execute("""
                SELECT period, SUM(input), SUM(output), SUM(cache_read),
                       SUM(cache_write), SUM(reasoning), SUM(total), SUM(cost), SUM(messages)
                FROM usage
                WHERE period_type='month'%s%s
                GROUP BY period ORDER BY period
            """ % (ex_clause, departed_clause), ex_params).fetchall()
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

    def _ai_usage(self, conn, qs, auth_user=None):
        """AI 用量接口(喂 Hermes skill / dashboard)。

        - ?user=<邮箱或登录名>&days=N → 该人窗口内 token 汇总 + 每日明细。
          登录名(无 @)自动补公司域名 AI_EMAIL_DOMAIN; 邮箱大小写不敏感。
          查不到人不报 404, 返回 total_tokens=0/daily=[]。
        - 不传 user → 窗口内按人 SUM 的 top-N 个人榜(默认排除离职; ?show_departed=1 纳入)。
        窗口语义同其它榜: ?from=&to= 优先, 否则 ?days=N, 默认近 30 天(只看 day 桶)。
        每条响应都带数据时间戳: latest_usage_date(数据覆盖到哪天) + generated_at。
        """
        if auth_user is not None:
            self._scope_user = auth_user
        scope_user = getattr(self, "_scope_user", None)
        latest = conn.execute(
            "SELECT max(period) FROM usage WHERE period_type='day'").fetchone()[0]
        now_iso = datetime.datetime.now().isoformat(timespec="seconds")

        departed_lower = {(e or "").lower() for e in _departed_set(conn)}
        PERSON_FILTER = (" AND source != 'litellm_agent'"
                         " AND email NOT LIKE 'litellm-key:%%'"
                         " AND email NOT LIKE 'litellm-user:%%'")

        # 唯一口径: 直接复用 /v1/leaderboard 的个人榜计算(网关实销 + 飞书点 + 订阅费窗口摊销)。
        # 窗口用与 leaderboard 完全相同的 _range_clause 语义 —— 不自加 to 上界, 否则 days=N 两接口
        # 会因 future-dated 行而漂(评审 #4)。无任何窗口参数时默认近 30 天(leaderboard 默认 lifetime)。
        eqs = dict(qs)
        if not (qs.get("from") or qs.get("to") or qs.get("days")):
            eqs["days"] = ["30"]
        board = _personal_board_rows(conn, eqs, auth_user=scope_user)
        dwhere, dparams = _range_clause(eqs, "")     # 个人榜同一窗口子句(每日明细复用)
        frng, fparams = _feishu_range(eqs)

        # 展示用窗口(days=N 无上界 → from=cutoff, to=null, 与 leaderboard 一致)。
        frm_q = (qs.get("from") or [None])[0]
        to_q = (qs.get("to") or [None])[0]
        days_disp = None
        if not (frm_q or to_q):
            raw = (qs.get("days") or [None])[0]
            try:
                days_disp = int(raw) if raw not in (None, "", "all") else 30
            except (TypeError, ValueError):
                days_disp = 30
            if days_disp <= 0:
                days_disp = 30
            frm_q = (datetime.date.today() - datetime.timedelta(days=days_disp - 1)).isoformat()
        window = {"days": days_disp, "from": frm_q, "to": to_q}

        user = (qs.get("user") or [None])[0]
        if user and user.strip():
            email = user.strip().lower()
            if "@" not in email:
                email = "%s@%s" % (email, AI_EMAIL_DOMAIN)
            if scope_user and not scope_user.get("is_admin"):
                prof = conn.execute(
                    "SELECT COALESCE(MAX(effective_dept), ''), COALESCE(MAX(dept), '')"
                    " FROM people WHERE lower(email)=?",
                    (email,),
                ).fetchone()
                scope_dept = _scope_dept(email, (prof[1] if prof else "") or "", None,
                                         (prof[0] if prof else "") or "")
                if not email_in_scope(scope_user, email, scope_dept):
                    return self._send(403, {"error": "forbidden for requested user"})
            is_departed = email in departed_lower
            # 大小写不敏感聚合: 防 DB 里同一人有大小写不同的 email 行 → total 与 daily 分裂(评审 #4)。
            matches = [r for r in board if (r["email"] or "").lower() == email]
            if not matches:
                # 不在榜(窗口内无用量, 或默认被排除的离职者) → 0/空; departed 标记仍给。
                prof = conn.execute(
                    "SELECT MAX(name), MAX(dept) FROM people WHERE lower(email)=?",
                    (email,)).fetchone()
                name = (prof[0] if prof else None) or None
                dept = (prof[1] if prof else None) or None
                total_tokens, cost_usd, daily = 0, 0, []
            else:
                total_tokens = sum(r["tokens"] or 0 for r in matches)
                cost_usd = round(sum(float(r["cost"] or 0) for r in matches), 4)
                name = matches[0].get("name") or None
                dept = matches[0].get("dept") or None
                # 每日明细(token 维度: 当天 SUM(total) + 飞书点, 和 == total_tokens 自洽; 同窗口子句)。
                # cost 含订阅月费摊销, 不可按天拆, 故 daily 只给 token。
                day_map = {}
                for p, tot in conn.execute(
                        "SELECT period, SUM(total) FROM usage WHERE %s%s AND lower(email)=? "
                        "GROUP BY period" % (dwhere, PERSON_FILTER),
                        dparams + [email]).fetchall():
                    day_map[p] = day_map.get(p, 0) + (tot or 0)
                for ud, cr in conn.execute(
                        "SELECT usage_date, SUM(credits) FROM feishu_member "
                        "WHERE 1=1%s AND lower(email)=? GROUP BY usage_date" % frng,
                        fparams + [email]).fetchall():
                    if cr:
                        day_map[ud] = day_map.get(ud, 0) + cr
                daily = [{"date": d, "total_tokens": day_map[d]} for d in sorted(day_map)]
            return self._send(200, {
                "user": email, "name": name, "dept": dept,
                "departed": is_departed,
                "window": window,
                "total_tokens": total_tokens, "cost_usd": cost_usd,
                "daily": daily,
                "latest_usage_date": latest, "generated_at": now_iso,
            })

        # 不传 user → 整张个人榜 top-N(已含飞书/订阅, 已按 tokens 降序)。
        try:
            limit = int((qs.get("limit") or ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        if limit <= 0:
            limit = 50
        ranking = [{"user": r["email"], "name": r.get("name") or None,
                    "dept": r.get("dept") or None,
                    "total_tokens": r["tokens"], "cost_usd": r["cost"]}
                   for r in board[:limit]]
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

        qs = qs or {}
        excluded_clause, excluded_params = _excluded_filter(qs)
        include_excluded = _include_excluded(qs)

        lifetime_row = conn.execute("""
            SELECT COUNT(DISTINCT email),
                   COUNT(DISTINCT CASE WHEN dept != '' THEN dept END),
                   COUNT(DISTINCT client),
                   COALESCE(SUM(total),0), COALESCE(SUM(cost),0),
                   COALESCE(SUM(messages),0), COALESCE(SUM(cache_read),0),
                   COALESCE(SUM(cache_write),0), COALESCE(SUM(input),0),
                   COALESCE(SUM(output),0)
            FROM usage WHERE period_type='lifetime'%s
        """ % excluded_clause, excluded_params).fetchone()
        day_row = conn.execute("""
            SELECT MIN(period), MAX(period), COUNT(DISTINCT period),
                   COUNT(DISTINCT email), COUNT(*),
                   COALESCE(SUM(total),0), COALESCE(SUM(cost),0)
            FROM usage WHERE period_type='day'%s
        """ % excluded_clause, excluded_params).fetchone()
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
        idle_where, idle_params = _range_clause(qs)
        idle_usage_rows = conn.execute(
            "SELECT DISTINCT email FROM usage "
            "WHERE %s AND source != 'litellm_agent' AND COALESCE(email, '') != ''%s"
            % (idle_where, excluded_clause),
            idle_params + excluded_params,
        ).fetchall()
        usage_emails = {r[0] for r in idle_usage_rows if r and r[0]}
        # 飞书 AI 权益点数也算「用量」：纯飞书用户已进个人榜(计订阅费),不能同时再算闲置。
        try:
            frng, fparams = _feishu_range(qs)
            for fr in conn.execute(
                    "SELECT DISTINCT email FROM feishu_member WHERE credits>0%s%s"
                    % (frng, excluded_clause),
                    fparams + excluded_params).fetchall():
                if fr and fr[0]:
                    usage_emails.add(fr[0])
        except Exception:
            pass  # feishu_member 表不存在(未启用飞书采集)时跳过
        today_d = datetime.date.today()
        idle_win_s, idle_win_e = _window_dates(qs) or (today_d, today_d)
        idle_fee_by = {}
        idle_emails = set()
        for email, subs in subs_by_email.items():
            if not include_excluded and (email or "").lower() in LEADERBOARD_EXCLUDE_EMAILS:
                continue
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
                WHERE period_type='day' AND period >= date(?, '-6 day')%s
            """ % excluded_clause, [max_date] + excluded_params).fetchone()
        else:
            last7_row = (0, 0, 0)

        source_rows = conn.execute("""
            SELECT source, COUNT(DISTINCT email), COALESCE(SUM(total),0), COALESCE(SUM(cost),0)
            FROM usage
            WHERE period_type='lifetime'%s
            GROUP BY source
            ORDER BY COALESCE(SUM(total),0) DESC
        """ % excluded_clause, excluded_params).fetchall()
        client_rows = conn.execute("""
            SELECT client, COUNT(DISTINCT email), COALESCE(SUM(total),0), COALESCE(SUM(cost),0)
            FROM usage
            WHERE period_type='lifetime'%s
            GROUP BY client
            ORDER BY COALESCE(SUM(total),0) DESC
        """ % excluded_clause, excluded_params).fetchall()

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
        feishu_sync = _feishu_sync_health(conn)
        if not feishu_sync["last_success"]:
            sync_availability = "pending"
            sync_value = "未同步"
        elif feishu_sync["status"] == "failure":
            sync_availability = "partial"
            sync_value = "同步失败"
        elif (feishu_sync["production_enablement_blocked"]
              or feishu_sync["visibility_warnings_count"]):
            sync_availability = "partial"
            sync_value = "覆盖率 {:.1f}%".format(
                feishu_sync["resolved_business_outsourcing_rate"] * 100.0)
        else:
            sync_availability = "computed"
            sync_value = "同步正常"

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
                "id": "feishu_directory_sync_health",
                "family": "Feishu org source of truth",
                "label": "飞书组织同步健康",
                "value": sync_value,
                "status": sync_availability,
                "availability": sync_availability,
                "benchmark": "组织真源需要展示最后成功同步、失败原因、可见性缺口和业务外包归因覆盖率。",
                "detail": "最后成功 {}；最后尝试 {}；visibility warnings {}；业务外包归因覆盖率 {:.1f}% / 阈值 {:.1f}%；roll-up {}。{}".format(
                    feishu_sync["last_success"] or "暂无",
                    feishu_sync["last_attempt"] or "暂无",
                    _fmt_int(feishu_sync["visibility_warnings_count"]),
                    feishu_sync["resolved_business_outsourcing_rate"] * 100.0,
                    feishu_sync["min_required_rate"] * 100.0,
                    "已启用" if feishu_sync["business_rollup_enabled"] else "未启用",
                    ("错误: " + feishu_sync["last_error"]) if feishu_sync["last_error"] else "",
                ),
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
                "feishu_directory_sync": feishu_sync,
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
        feishu_sync = _feishu_sync_health(conn)
        scope_user = getattr(self, "_scope_user", None)
        if scope_user and not scope_user.get("is_admin"):
            feishu_sync = _redacted_feishu_sync_health(feishu_sync)
        self._send(200, {
            "min_date": (row[0] if row else "") or "",
            "max_date": (row[1] if row else "") or "",
            "last_report": (last[0] if last else "") or "",
            "feishu_directory_sync": feishu_sync,
        })

    def _raw(self, conn):
        """明细（调试用，LIMIT 100）。"""
        usage_cols = _table_columns(conn, "usage")
        audit_select = []
        for col in ("raw_dept", "effective_dept", "spend_bucket", "attribution_source"):
            if col in usage_cols:
                audit_select.append("COALESCE(%s,'') AS %s" % (col, col))
            else:
                audit_select.append("'' AS %s" % col)
        rows = conn.execute("""
            SELECT email, period_type, period, source, client, provider, model,
                   input, output, cache_read, cache_write, reasoning, total, cost, messages,
                   %s
            FROM usage ORDER BY total DESC LIMIT 100
        """ % ", ".join(audit_select)).fetchall()
        cols = ["email", "period_type", "period", "source", "client", "provider", "model",
                "input", "output", "cache_read", "cache_write", "reasoning", "total", "cost",
                "messages", "raw_dept", "effective_dept", "spend_bucket", "attribution_source"]
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
