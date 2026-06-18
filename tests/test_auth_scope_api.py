import importlib
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import dev_collector  # noqa: E402


class _Handler:
    def _send(self, code, obj):
        self.code = code
        self.payload = obj


def _reload(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADERBOARD_EXCLUDE_EMAILS", "")
    dc = importlib.reload(dev_collector)
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    return dc


def _usage(dc, conn, email, dept, tokens):
    conn.execute(
        dc._UPSERT_SQL,
        (
            email,
            dept,
            "lifetime",
            "all",
            "subscription",
            "Claude Code",
            "",
            "model-x",
            tokens,
            0,
            0,
            0,
            0,
            tokens,
            1.0,
            1,
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO people(email,name,avatar,dept,effective_dept,spend_bucket,source)"
        " VALUES(?,?,?,?,?,?,?)",
        (email, email.split("@")[0], "", dept, dept, "employee_staff_outsourcing", "feishu"),
    )


def _role(email, roles, scope, owned=()):
    return {
        "email": email,
        "roles": list(roles),
        "is_admin": "admin" in roles,
        "scope": scope,
        "owned_departments": list(owned),
    }


def test_member_leaderboard_only_returns_self(monkeypatch, tmp_path):
    dc = _reload(monkeypatch, tmp_path)
    conn = dc.db()
    try:
        _usage(dc, conn, "alice@keep.com", "Keep/技术平台部/固件组", 100)
        _usage(dc, conn, "bob@keep.com", "Keep/运动消费事业部/市场营销部", 200)
        conn.commit()
        h = _Handler()

        dc.H._leaderboard(h, conn, {}, auth_user=_role("alice@keep.com", ["member"], "self"))
    finally:
        conn.close()

    assert h.code == 200
    assert [r["email"] for r in h.payload["leaderboard"]] == ["alice@keep.com"]


def test_owner_leaderboard_returns_owned_subtree_only(monkeypatch, tmp_path):
    dc = _reload(monkeypatch, tmp_path)
    conn = dc.db()
    try:
        _usage(dc, conn, "alice@keep.com", "Keep/技术平台部/固件组", 100)
        _usage(dc, conn, "bob@keep.com", "Keep/运动消费事业部/市场营销部", 200)
        conn.commit()
        h = _Handler()

        dc.H._leaderboard(
            h,
            conn,
            {},
            auth_user=_role("lead@keep.com", ["department_owner"], "department", ["Keep/技术平台部"]),
        )
    finally:
        conn.close()

    assert h.code == 200
    assert [r["email"] for r in h.payload["leaderboard"]] == ["alice@keep.com"]


def test_owner_teams_returns_owned_subtree_rollup_only(monkeypatch, tmp_path):
    dc = _reload(monkeypatch, tmp_path)
    monkeypatch.setattr(
        dc,
        "_dept_headcount_map",
        lambda: {
            "Keep/技术平台部/固件组": 2,
            "Keep/运动消费事业部/市场营销部": 3,
        },
    )
    conn = dc.db()
    try:
        _usage(dc, conn, "alice@keep.com", "Keep/技术平台部/固件组", 100)
        _usage(dc, conn, "bob@keep.com", "Keep/运动消费事业部/市场营销部", 200)
        conn.commit()
        h = _Handler()

        dc.H._teams(
            h,
            conn,
            {},
            auth_user=_role("lead@keep.com", ["department_owner"], "department", ["Keep/技术平台部"]),
        )
    finally:
        conn.close()

    assert h.code == 200
    teams = {r["dept"]: r for r in h.payload["teams"]}
    assert teams["Keep"]["tokens"] == 100
    assert "Keep/技术平台部/固件组" in teams
    assert "Keep/运动消费事业部" not in teams


def test_member_cannot_read_other_user_ai_usage(monkeypatch, tmp_path):
    dc = _reload(monkeypatch, tmp_path)
    conn = dc.db()
    try:
        _usage(dc, conn, "alice@keep.com", "Keep/技术平台部/固件组", 100)
        _usage(dc, conn, "bob@keep.com", "Keep/运动消费事业部/市场营销部", 200)
        conn.commit()
        h = _Handler()

        dc.H._ai_usage(
            h,
            conn,
            {"user": ["bob@keep.com"]},
            auth_user=_role("alice@keep.com", ["member"], "self"),
        )
    finally:
        conn.close()

    assert h.code == 403
    assert h.payload == {"error": "forbidden for requested user"}


def test_member_ai_usage_board_is_filtered_to_self(monkeypatch, tmp_path):
    dc = _reload(monkeypatch, tmp_path)
    conn = dc.db()
    try:
        _usage(dc, conn, "alice@keep.com", "Keep/技术平台部/固件组", 100)
        _usage(dc, conn, "bob@keep.com", "Keep/运动消费事业部/市场营销部", 200)
        conn.commit()
        h = _Handler()

        dc.H._ai_usage(
            h,
            conn,
            {"days": ["all"]},
            auth_user=_role("alice@keep.com", ["member"], "self"),
        )
    finally:
        conn.close()

    assert h.code == 200
    assert [r["user"] for r in h.payload["ranking"]] == ["alice@keep.com"]
