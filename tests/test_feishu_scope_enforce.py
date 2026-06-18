# -*- coding: utf-8 -*-
"""Task 8 — row-level scope enforcement through the real data handlers.

Seeds a small usage/people/roles DB and drives _leaderboard / _cursor / _teams /
_trend with member / owner / admin scope, asserting each caller sees only their
visible rows. No sockets, no network.
"""
import importlib
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))
import dev_collector  # noqa: E402

_DAY = "2026-06-18"


@pytest.fixture
def dc(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADERBOARD_EXCLUDE_EMAILS", "sunke@keep.com")
    m = importlib.reload(dev_collector)
    monkeypatch.setattr(m, "DB", str(tmp_path / "tok.db"))
    return m


def _seed(dc, conn):
    def usage(email, dept, tokens, client="Claude Code"):
        source = "cursor" if client == "Cursor" else "litellm"
        conn.execute(dc._UPSERT_SQL, (
            email, dept, "lifetime", "all", source, client, "",
            "model-x", tokens, 0, 0, 0, 0, tokens, 1.0, 1))

    def person(email, name, dept):
        conn.execute("INSERT OR REPLACE INTO people(email,name,avatar,dept,effective_dept)"
                     " VALUES(?,?,?,?,?)", (email, name, "", dept, dept))

    person("emp@keep.com", "员工", "技术平台部/固件组")
    person("peer@keep.com", "同组", "技术平台部/固件组")
    person("lead@keep.com", "组长", "技术平台部")
    person("other@keep.com", "他部门", "运动消费事业部/市场营销部")
    for e, d in [("emp@keep.com", "技术平台部/固件组"),
                 ("peer@keep.com", "技术平台部/固件组"),
                 ("lead@keep.com", "技术平台部"),
                 ("other@keep.com", "运动消费事业部/市场营销部")]:
        usage(e, d, 100)
        usage(e, d, 50, client="Cursor")
    # lead is department_owner of 技术平台部
    conn.execute("INSERT INTO roles(email,role,dept_id,dept_path,source,updated_at)"
                 " VALUES('lead@keep.com','department_owner','d_tech','技术平台部','t','t')")
    conn.commit()


def _seed_feishu(conn):
    rows = [
        ("emp@keep.com", "员工", "技术平台部/固件组", "aily_credits", 11, _DAY, "", ""),
        ("peer@keep.com", "同组", "技术平台部/固件组", "AI_credits", 13, _DAY, "", ""),
        ("lead@keep.com", "组长", "技术平台部", "aily_credits", 17, _DAY, "", ""),
        ("other@keep.com", "他部门", "运动消费事业部/市场营销部", "aily_credits", 19, _DAY, "", ""),
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO feishu_member"
        "(email,name,dept,feature_key,credits,usage_date,avatar,entity_id)"
        " VALUES(?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.execute(
        "INSERT OR REPLACE INTO feishu_quota"
        "(feature_key,quota,used,remain,period_start,period_end)"
        " VALUES('aily_credits',1000,60,940,?,?)",
        (_DAY, _DAY),
    )
    conn.execute(
        "INSERT OR REPLACE INTO feishu_trend"
        "(usage_date,biz_type,credits,user_count) VALUES(?,?,?,?)",
        (_DAY, "aily", 60, 4),
    )
    conn.commit()


def _handler(dc, scope_user=None):
    h = dc.H.__new__(dc.H)
    h._scope_user = scope_user
    h.captured = {}
    h._send = lambda code, obj: h.captured.update(code=code, obj=obj)
    return h


def _emails(rows):
    return {r.get("email") for r in rows}


def _depts(rows):
    return {r.get("dept") for r in rows}


# --------------------------------------------------------------------------- #
def test_member_leaderboard_sees_only_self(dc):
    conn = dc.db()
    _seed(dc, conn)
    me = dc._user_roles(conn, "emp@keep.com")
    assert me["scope"] == "self"
    h = _handler(dc, me)
    dc.H._leaderboard(h, conn, {})
    assert h.captured["code"] == 200
    assert _emails(h.captured["obj"]["leaderboard"]) == {"emp@keep.com"}


def test_admin_leaderboard_sees_everyone(dc):
    conn = dc.db()
    _seed(dc, conn)
    # admin path: do_GET sets _scope_user=None for admins -> no filtering
    h = _handler(dc, None)
    dc.H._leaderboard(h, conn, {})
    got = _emails(h.captured["obj"]["leaderboard"])
    assert {"emp@keep.com", "peer@keep.com", "other@keep.com"} <= got


def test_member_cursor_sees_only_self(dc):
    conn = dc.db()
    _seed(dc, conn)
    me = dc._user_roles(conn, "emp@keep.com")
    h = _handler(dc, me)
    dc.H._cursor(h, conn, {})
    assert _emails(h.captured["obj"]["cursor"]) == {"emp@keep.com"}


def test_owner_teams_sees_only_own_subtree(dc):
    conn = dc.db()
    _seed(dc, conn)
    owner = dc._user_roles(conn, "lead@keep.com")
    assert owner["scope"] == "department"
    h = _handler(dc, owner)
    dc.H._teams(h, conn, {})
    depts = _depts(h.captured["obj"]["teams"])
    # Keep root is retained as the scoped roll-up parent; leaf departments must
    # stay within 技术平台部 and never leak the sibling department.
    assert depts, "owner should see their own subtree"
    for d in depts:
        key = dc._canonical_dept_key(d)
        assert key == "" or key == "技术平台部" or key.startswith("技术平台部/"), d
    assert not any("运动消费" in (d or "") for d in depts)


def test_owner_teams_excludes_other_department(dc):
    conn = dc.db()
    _seed(dc, conn)
    owner = dc._user_roles(conn, "lead@keep.com")
    h = _handler(dc, owner)
    dc.H._teams(h, conn, {})
    assert all("运动消费" not in (d or "") for d in _depts(h.captured["obj"]["teams"]))


def test_member_trend_no_filter_collapses_to_self(dc):
    conn = dc.db()
    _seed(dc, conn)
    me = dc._user_roles(conn, "emp@keep.com")
    h = _handler(dc, me)
    dc.H._trend(h, conn, {})  # no ?email -> forced to self
    assert h.captured["obj"]["email"] == "emp@keep.com"


def test_member_trend_other_email_is_403(dc):
    conn = dc.db()
    _seed(dc, conn)
    me = dc._user_roles(conn, "emp@keep.com")
    h = _handler(dc, me)
    dc.H._trend(h, conn, {"email": ["other@keep.com"]})
    assert h.captured["code"] == 403


def test_owner_trend_subordinate_allowed(dc):
    conn = dc.db()
    _seed(dc, conn)
    owner = dc._user_roles(conn, "lead@keep.com")
    h = _handler(dc, owner)
    dc.H._trend(h, conn, {"email": ["emp@keep.com"]})  # emp is in 技术平台部 subtree
    assert h.captured["code"] == 200
    assert h.captured["obj"]["email"] == "emp@keep.com"


def test_owner_trend_outside_scope_is_403(dc):
    conn = dc.db()
    _seed(dc, conn)
    owner = dc._user_roles(conn, "lead@keep.com")
    h = _handler(dc, owner)
    dc.H._trend(h, conn, {"email": ["other@keep.com"]})
    assert h.captured["code"] == 403


def test_member_feishu_sees_only_self_and_no_global_totals(dc):
    conn = dc.db()
    _seed(dc, conn)
    _seed_feishu(conn)
    me = dc._user_roles(conn, "emp@keep.com")
    h = _handler(dc, me)
    dc.H._feishu(h, conn, {"from": [_DAY], "to": [_DAY]})
    assert h.captured["code"] == 200
    assert _emails(h.captured["obj"]["members"]) == {"emp@keep.com"}
    assert h.captured["obj"]["dept"] == [{
        "dept": "技术平台部/固件组", "credits": 11, "people": 1
    }]
    assert h.captured["obj"]["quota"] == []
    assert h.captured["obj"]["trend"] == []


def test_owner_feishu_sees_only_owned_subtree(dc):
    conn = dc.db()
    _seed(dc, conn)
    _seed_feishu(conn)
    owner = dc._user_roles(conn, "lead@keep.com")
    h = _handler(dc, owner)
    dc.H._feishu(h, conn, {"from": [_DAY], "to": [_DAY]})
    assert h.captured["code"] == 200
    assert _emails(h.captured["obj"]["members"]) == {
        "emp@keep.com", "peer@keep.com", "lead@keep.com"
    }
    assert all("运动消费" not in (d or "") for d in _depts(h.captured["obj"]["dept"]))
    assert h.captured["obj"]["quota"] == []
    assert h.captured["obj"]["trend"] == []


def test_gate_allows_member_aiusage_and_feishu(dc):
    member = {"email": "emp@keep.com", "roles": ["member"], "is_admin": False,
              "scope": "self", "owned_departments": []}
    assert dc.authorize_request(member, "/v1/ai/usage", True) == "allow"
    assert dc.authorize_request(member, "/v1/feishu", True) == "allow"
    # owner gets team view, member does not
    owner = {"email": "lead@keep.com", "roles": ["department_owner"], "is_admin": False,
             "scope": "department", "owned_departments": ["技术平台部"]}
    assert dc.authorize_request(owner, "/v1/teams", True) == "allow"
    assert dc.authorize_request(member, "/v1/teams", True) == "403"
