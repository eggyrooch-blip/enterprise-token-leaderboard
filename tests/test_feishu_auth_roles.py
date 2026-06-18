import importlib
import pathlib
import sqlite3
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import dev_collector  # noqa: E402


def test_sunke_is_always_super_admin(monkeypatch):
    monkeypatch.setenv("AUTH_ADMIN_EMAILS", "ops@keep.com")
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")

    dc.ensure_auth_tables(conn)
    conn.execute(
        "INSERT INTO role_overrides(email, role, dept_id, action, reason)"
        " VALUES(?,?,?,?,?)",
        ("sunke@keep.com", "admin", "", "deny", "misconfigured deny must not win"),
    )

    sunke = dc._user_roles(conn, "sunke@keep.com")
    ops = dc._user_roles(conn, "ops@keep.com")

    assert sunke["is_admin"] is True
    assert "admin" in sunke["roles"]
    assert sunke["scope"] == "global"
    assert ops["is_admin"] is True


def test_role_override_can_deny_feishu_derived_owner(monkeypatch):
    monkeypatch.setenv("AUTH_ADMIN_EMAILS", "")
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    dc.ensure_auth_tables(conn)
    conn.execute(
        "INSERT INTO roles(email, role, dept_id, dept_path, source, updated_at)"
        " VALUES(?,?,?,?,?,?)",
        ("owner@keep.com", "department_owner", "d1", "Keep/A", "feishu", "2026-06-18"),
    )
    conn.execute(
        "INSERT INTO role_overrides(email, role, dept_id, action, reason)"
        " VALUES(?,?,?,?,?)",
        ("owner@keep.com", "department_owner", "d1", "deny", "temporary removal"),
    )
    conn.commit()

    roles = dc._user_roles(conn, "owner@keep.com")

    assert "department_owner" not in roles["roles"]
    assert roles["owned_departments"] == []


def test_deny_override_wins_over_allow_override(monkeypatch):
    monkeypatch.setenv("AUTH_ADMIN_EMAILS", "")
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    dc.ensure_auth_tables(conn)
    conn.execute(
        "INSERT INTO role_overrides(email, role, dept_id, action, reason)"
        " VALUES(?,?,?,?,?)",
        ("ops@keep.com", "admin", "", "allow", "temporary"),
    )
    conn.execute(
        "INSERT INTO role_overrides(email, role, dept_id, action, reason)"
        " VALUES(?,?,?,?,?)",
        ("ops@keep.com", "admin", "", "deny", "explicit deny"),
    )
    conn.commit()

    roles = dc._user_roles(conn, "ops@keep.com")

    assert "admin" not in roles["roles"]
    assert roles["scope"] == "self"


def test_department_owner_allow_override_without_role_row_does_not_create_empty_owner_scope(monkeypatch):
    monkeypatch.setenv("AUTH_ADMIN_EMAILS", "")
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    dc.ensure_auth_tables(conn)
    conn.execute(
        "INSERT INTO role_overrides(email, role, dept_id, action, reason)"
        " VALUES(?,?,?,?,?)",
        ("ops@keep.com", "department_owner", "d1", "allow", "no dept_path available"),
    )
    conn.commit()

    roles = dc._user_roles(conn, "ops@keep.com")

    assert "department_owner" not in roles["roles"]
    assert roles["owned_departments"] == []
    assert roles["scope"] == "self"


def test_department_owner_allow_override_uses_departments_path(monkeypatch):
    monkeypatch.setenv("AUTH_ADMIN_EMAILS", "")
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    dc.ensure_auth_tables(conn)
    conn.execute(
        "CREATE TABLE departments(dept_id TEXT PRIMARY KEY, path TEXT)"
    )
    conn.execute(
        "INSERT INTO departments(dept_id, path) VALUES(?,?)",
        ("d1", "Keep/A"),
    )
    conn.execute(
        "INSERT INTO role_overrides(email, role, dept_id, action, reason)"
        " VALUES(?,?,?,?,?)",
        ("ops@keep.com", "department_owner", "d1", "allow", "temporary owner"),
    )
    conn.commit()

    roles = dc._user_roles(conn, "ops@keep.com")

    assert "department_owner" in roles["roles"]
    assert roles["owned_departments"] == ["Keep/A"]
    assert roles["scope"] == "department"
