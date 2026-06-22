# -*- coding: utf-8 -*-
"""Tests for the Feishu OAuth + session auth layer in dev_collector.py.

Pure/unit level — no sockets, no live network. The HTTP handler is exercised
via a socket-free fake instance that captures responses.
"""
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))
import dev_collector as dc  # noqa: E402


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    dc.ensure_auth_tables(c)
    c.execute("CREATE TABLE IF NOT EXISTS people(email TEXT PRIMARY KEY, name TEXT,"
              " avatar TEXT, dept TEXT)")
    dc._ensure_people_directory_columns(c)
    return c


class _Headers(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def _handler(cookie=None):
    h = dc.H.__new__(dc.H)
    h.headers = _Headers({"Cookie": cookie} if cookie else {})
    h.captured = []
    h._send = lambda code, obj: h.captured.append(("send", code, obj))
    h._send_redirect = (lambda location, set_cookie=None, clear_cookie=False:
                        h.captured.append(("redirect", location, set_cookie, clear_cookie)))
    return h


def _route_handler(path="/", cookie=None):
    h = _handler(cookie)
    h.path = path
    h._dashboard = lambda: h.captured.append(("dashboard",))
    return h


# --------------------------------------------------------------------------- #
# state: one-time-use + expiry
# --------------------------------------------------------------------------- #
def test_state_is_one_time_use(conn):
    s = dc.create_oauth_state(conn, "/dashboard", now=1000)
    assert dc.consume_oauth_state(conn, s, now=1001) == "/dashboard"
    # second use is rejected
    assert dc.consume_oauth_state(conn, s, now=1002) is None


def test_state_expires(conn):
    s = dc.create_oauth_state(conn, "/", now=1000)
    assert dc.consume_oauth_state(conn, s, now=1000 + dc.STATE_TTL + 1) is None


def test_unknown_state_rejected(conn):
    assert dc.consume_oauth_state(conn, "nope", now=1) is None


def test_login_next_rejects_external_url(conn, monkeypatch):
    monkeypatch.setattr(dc, "FEISHU_APP_ID", "cli_test")
    monkeypatch.setattr(dc, "FEISHU_OAUTH_REDIRECT_URI", "https://example.com/v1/auth/callback")
    h = _handler()
    h._auth_login(conn, {"next": ["https://evil.example/phish"]})
    assert h.captured and h.captured[0][0] == "redirect"
    state = conn.execute("SELECT state FROM auth_states").fetchone()[0]
    assert dc.consume_oauth_state(conn, state) == "/"


def test_login_next_keeps_local_path_and_query(conn, monkeypatch):
    monkeypatch.setattr(dc, "FEISHU_APP_ID", "cli_test")
    monkeypatch.setattr(dc, "FEISHU_OAUTH_REDIRECT_URI", "https://example.com/v1/auth/callback")
    h = _handler()
    h._auth_login(conn, {"next": ["/dashboard?tab=team"]})
    state = conn.execute("SELECT state FROM auth_states").fetchone()[0]
    assert dc.consume_oauth_state(conn, state) == "/dashboard?tab=team"


def test_authorize_url_uses_current_feishu_code_endpoint(monkeypatch):
    monkeypatch.setattr(dc, "FEISHU_AUTH_HOST", "https://accounts.feishu.cn")
    monkeypatch.setattr(dc, "FEISHU_APP_ID", "cli_test")
    monkeypatch.setattr(dc, "FEISHU_OAUTH_REDIRECT_URI", "https://example.com/v1/auth/callback")

    url = dc.feishu_authorize_url("state-1")

    assert url.startswith("https://accounts.feishu.cn/open-apis/authen/v1/authorize?")
    assert "client_id=cli_test" in url
    assert "response_type=code" in url
    assert "state=state-1" in url


# --------------------------------------------------------------------------- #
# session lifecycle + expiry
# --------------------------------------------------------------------------- #
def test_session_roundtrip_and_expiry(conn):
    sid = dc.create_session(conn, "Leader@Keep.com", now=1000)
    assert dc.session_email(conn, sid, now=1001) == "leader@keep.com"
    # expired
    assert dc.session_email(conn, sid, now=1000 + dc.SESSION_TTL + 1) is None
    # expired session is deleted
    assert dc.session_email(conn, sid, now=1002) is None


def test_session_prefers_open_id_identity(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS feishu_users(open_id TEXT PRIMARY KEY, user_id TEXT,"
        " union_id TEXT, email TEXT, name TEXT, dept_id TEXT, dept_path TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO feishu_users(open_id,user_id,union_id,email,name,dept_path,status)"
        " VALUES('ou_emp','u_emp','on_emp','emp@keep.com','员工','技术平台部','active')"
    )
    sid = dc.create_session(
        conn,
        {"open_id": "ou_emp", "user_id": "u_emp", "union_id": "on_emp",
         "email": "", "name": "员工"},
        now=1000,
    )

    assert dc.session_identity(conn, sid, now=1001)["open_id"] == "ou_emp"
    assert dc.session_email(conn, sid, now=1001) == "emp@keep.com"


def test_session_unknown_sid(conn):
    assert dc.session_email(conn, "missing", now=1) is None
    assert dc.session_email(conn, None) is None


def test_secure_cookie_default_follows_https_redirect(monkeypatch):
    monkeypatch.delenv("AUTH_COOKIE_SECURE", raising=False)
    monkeypatch.setattr(dc, "FEISHU_OAUTH_REDIRECT_URI", "https://example.com/v1/auth/callback")
    assert dc._default_auth_cookie_secure() is True
    monkeypatch.setattr(dc, "FEISHU_OAUTH_REDIRECT_URI", "http://localhost:8090/v1/auth/callback")
    assert dc._default_auth_cookie_secure() is False


# --------------------------------------------------------------------------- #
# feishu_exchange_code: requires email, parses profile
# --------------------------------------------------------------------------- #
def test_exchange_code_returns_profile(monkeypatch):
    calls = []

    def fake(url, payload=None, headers=None):
        calls.append(url)
        if "oauth/token" in url:
            return {"code": 0, "data": {"user_access_token": "u-tok"}}
        return {"code": 0, "data": {"email": "Emp@Keep.com", "name": "员工",
                                    "open_id": "ou_emp"}}

    monkeypatch.setattr(dc, "_oauth_http_json", fake)
    info = dc.feishu_exchange_code("the-code")
    assert info["email"] == "emp@keep.com"
    assert info["open_id"] == "ou_emp"
    assert info["user_id"] == ""
    assert info["union_id"] == ""
    assert any("oauth/token" in u for u in calls)
    assert any("user_info" in u for u in calls)


def test_exchange_code_raises_on_token_error(monkeypatch):
    monkeypatch.setattr(dc, "_oauth_http_json",
                        lambda *a, **k: {"code": 99, "msg": "bad code"})
    with pytest.raises(RuntimeError):
        dc.feishu_exchange_code("x")


# --------------------------------------------------------------------------- #
# /v1/me
# --------------------------------------------------------------------------- #
def test_me_401_without_session(conn):
    h = _handler()
    h._me(conn)
    assert h.captured == [("send", 401, {"error": "not authenticated"})]


def test_me_returns_identity_with_session(conn):
    conn.execute("INSERT INTO people(email,name,dept) VALUES('emp@keep.com','员工','技术平台部')")
    sid = dc.create_session(conn, "emp@keep.com")
    h = _handler(cookie="%s=%s" % (dc.SESSION_COOKIE, sid))
    h._me(conn)
    kind, code, obj = h.captured[0]
    assert code == 200
    assert obj["email"] == "emp@keep.com"
    assert obj["roles"] == ["member"]
    assert obj["scope"] == "self"
    assert obj["is_admin"] is False


def test_me_returns_identity_with_open_id_session(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS feishu_users(open_id TEXT PRIMARY KEY, user_id TEXT,"
        " union_id TEXT, email TEXT, name TEXT, dept_id TEXT, dept_path TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO feishu_users(open_id,user_id,union_id,email,name,dept_path,status)"
        " VALUES('ou_emp','u_emp','on_emp','emp@keep.com','员工','技术平台部','active')"
    )
    sid = dc.create_session(conn, {"open_id": "ou_emp", "email": "", "name": "员工"})
    h = _handler(cookie="%s=%s" % (dc.SESSION_COOKIE, sid))
    h._me(conn)

    _, code, obj = h.captured[0]
    assert code == 200
    assert obj["open_id"] == "ou_emp"
    assert obj["user_id"] == "u_emp"
    assert obj["union_id"] == "on_emp"
    assert obj["email"] == "emp@keep.com"
    assert obj["name"] == "员工"


def test_me_reports_admin_for_super_admin(conn):
    sid = dc.create_session(conn, "sunke@keep.com")
    h = _handler(cookie="%s=%s" % (dc.SESSION_COOKIE, sid))
    h._me(conn)
    _, code, obj = h.captured[0]
    assert code == 200 and obj["is_admin"] is True and obj["scope"] == "global"


# --------------------------------------------------------------------------- #
# OAuth callback flow
# --------------------------------------------------------------------------- #
def test_callback_with_email_creates_session_and_cookie(conn, monkeypatch):
    monkeypatch.setattr(dc, "feishu_exchange_code",
                        lambda code: {"email": "emp@keep.com", "name": "员工", "open_id": "ou_emp",
                                      "user_id": "u_emp", "union_id": "on_emp"})
    state = dc.create_oauth_state(conn, "/dashboard")
    h = _handler()
    h._auth_callback(conn, {"code": ["c"], "state": [state]})
    kind, location, set_cookie, clear = h.captured[0]
    assert kind == "redirect" and location == "/dashboard"
    assert set_cookie  # a session id cookie was set
    ident = dc.session_identity(conn, set_cookie)
    assert ident["open_id"] == "ou_emp"
    assert ident["user_id"] == "u_emp"
    assert ident["union_id"] == "on_emp"
    assert dc.session_email(conn, set_cookie) == "emp@keep.com"


def test_callback_open_id_only_creates_session_when_directory_can_resolve(conn, monkeypatch):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS feishu_users(open_id TEXT PRIMARY KEY, user_id TEXT,"
        " union_id TEXT, email TEXT, name TEXT, dept_id TEXT, dept_path TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO feishu_users(open_id,user_id,union_id,email,name,dept_path,status)"
        " VALUES('ou_x','u_x','on_x','vendor@keep.com','供应商','合作商/W','active')"
    )
    monkeypatch.setattr(dc, "feishu_exchange_code",
                        lambda code: {"email": "", "name": "供应商", "open_id": "ou_x",
                                      "user_id": "", "union_id": "on_x"})
    state = dc.create_oauth_state(conn, "/")
    h = _handler()
    h._auth_callback(conn, {"code": ["c"], "state": [state]})

    kind, location, set_cookie, clear = h.captured[0]
    assert kind == "redirect" and location == "/"
    assert dc.session_identity(conn, set_cookie)["open_id"] == "ou_x"
    assert dc.session_email(conn, set_cookie) == "vendor@keep.com"


def test_callback_missing_stable_identity_is_403_no_session(conn, monkeypatch):
    monkeypatch.setattr(dc, "feishu_exchange_code",
                        lambda code: {"email": "", "name": "供应商", "open_id": ""})
    state = dc.create_oauth_state(conn, "/")
    h = _handler()
    h._auth_callback(conn, {"code": ["c"], "state": [state]})
    assert h.captured == [("send", 403,
                           {"error": "feishu profile has no stable identity; access denied"})]
    # no session rows created
    assert conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0


def test_callback_reused_state_is_400(conn, monkeypatch):
    monkeypatch.setattr(dc, "feishu_exchange_code",
                        lambda code: {"email": "emp@keep.com"})
    state = dc.create_oauth_state(conn, "/", now=1000)
    h1 = _handler()
    h1._auth_callback(conn, {"code": ["c"], "state": [state]})  # consumes state
    h2 = _handler()
    h2._auth_callback(conn, {"code": ["c"], "state": [state]})  # reuse
    assert h2.captured == [("send", 400, {"error": "invalid or expired state"})]


def test_callback_missing_params_is_400(conn):
    h = _handler()
    h._auth_callback(conn, {"code": [""], "state": [""]})
    assert h.captured[0][1] == 400


# --------------------------------------------------------------------------- #
# scope predicate
# --------------------------------------------------------------------------- #
def _role(email, roles, scope, owned=()):
    return {"email": email, "roles": list(roles), "is_admin": "admin" in roles,
            "scope": scope, "owned_departments": list(owned)}


def test_email_in_scope_admin_sees_all():
    admin = _role("a@keep.com", ["admin"], "global")
    assert dc.email_in_scope(admin, "anyone@keep.com", "运动消费事业部/市场营销部")


def test_email_in_scope_member_sees_only_self():
    m = _role("emp@keep.com", ["member"], "self")
    assert dc.email_in_scope(m, "emp@keep.com", "技术平台部")
    assert not dc.email_in_scope(m, "other@keep.com", "技术平台部")


def test_email_in_scope_owner_sees_subtree_and_self():
    o = _role("lead@keep.com", ["department_owner"], "department",
              owned=["技术平台部"])
    assert dc.email_in_scope(o, "x@keep.com", "技术平台部/固件组")  # subtree
    assert dc.email_in_scope(o, "x@keep.com", "Keep/技术平台部")    # canonical strips Keep/
    assert not dc.email_in_scope(o, "x@keep.com", "运动消费事业部")  # outside
    assert dc.email_in_scope(o, "lead@keep.com", "运动消费事业部")   # always self


# --------------------------------------------------------------------------- #
# authorization gate (shadow vs enforce)
# --------------------------------------------------------------------------- #
def test_gate_shadow_allows_everything():
    assert dc.authorize_request(None, "/v1/leaderboard", enforced=False) == "allow"


def test_gate_enforce_unauthenticated_is_401():
    assert dc.authorize_request(None, "/v1/leaderboard", enforced=True) == "401"
    assert dc.authorize_request(None, "/v1/agent_leaderboard", enforced=True) == "401"


def test_gate_enforce_admin_allowed_everywhere():
    admin = _role("a@keep.com", ["admin"], "global")
    assert dc.authorize_request(admin, "/v1/raw", enforced=True) == "allow"
    assert dc.authorize_request(admin, "/v1/governance_metrics", enforced=True) == "allow"


def test_gate_enforce_member_allowed_scoped_self_routes_but_not_admin_routes():
    m = _role("emp@keep.com", ["member"], "self")
    assert dc.authorize_request(m, "/v1/leaderboard", enforced=True) == "allow"
    assert dc.authorize_request(m, "/v1/agent_leaderboard", enforced=True) == "allow"
    assert dc.authorize_request(m, "/v1/agent_owner_summary", enforced=True) == "allow"
    assert dc.authorize_request(m, "/v1/ai/usage", enforced=True) == "allow"
    assert dc.authorize_request(m, "/v1/breakdown", enforced=True) == "allow"
    assert dc.authorize_request(m, "/v1/teams", enforced=True) == "403"
    assert dc.authorize_request(m, "/v1/raw", enforced=True) == "403"


def test_gate_enforce_owner_allowed_team_and_person_routes():
    owner = _role("lead@keep.com", ["department_owner"], "department", ["Keep/A"])
    assert dc.authorize_request(owner, "/v1/leaderboard", enforced=True) == "allow"
    assert dc.authorize_request(owner, "/v1/teams", enforced=True) == "allow"
    assert dc.authorize_request(owner, "/v1/ai/usage", enforced=True) == "allow"
    assert dc.authorize_request(owner, "/v1/breakdown", enforced=True) == "allow"
    assert dc.authorize_request(owner, "/v1/raw", enforced=True) == "403"


def test_gate_never_touches_report_or_auth_routes():
    # routes not in DATA_ROUTES are always allowed by the gate
    assert dc.authorize_request(None, "/v1/tokscale/report", enforced=True) == "allow"
    assert dc.authorize_request(None, "/v1/me", enforced=True) == "allow"


def test_auth_enforced_reads_env(monkeypatch):
    monkeypatch.delenv("AUTH_ENFORCE", raising=False)
    assert dc.auth_enforced() is False
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    assert dc.auth_enforced() is True


def test_dashboard_redirects_to_login_when_enforced_without_session(conn, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setattr(dc, "db", lambda: conn)
    h = _route_handler("/")
    dc.H.do_GET(h)
    assert h.captured == [("redirect", "/v1/auth/login?next=%2F", None, False)]


def test_dashboard_serves_when_enforced_with_session(conn, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setattr(dc, "db", lambda: conn)
    sid = dc.create_session(conn, "emp@keep.com")
    h = _route_handler("/dashboard", cookie="%s=%s" % (dc.SESSION_COOKIE, sid))
    dc.H.do_GET(h)
    assert h.captured == [("dashboard",)]
