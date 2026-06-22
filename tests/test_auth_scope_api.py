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


def _usage(dc, conn, email, dept, tokens, client="Claude Code", model="model-x",
           raw_dept=None, effective_dept=None, spend_bucket="employee_staff_outsourcing"):
    conn.execute(
        dc._UPSERT_SQL,
        (
            email,
            dept,
            "lifetime",
            "all",
            "subscription",
            client,
            "",
            model,
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
        "UPDATE usage SET raw_dept=?, effective_dept=?, spend_bucket=?, attribution_source=?"
        " WHERE email=? AND dept=? AND period_type='lifetime' AND period='all'"
        " AND source='subscription' AND client=? AND model=?",
        (
            raw_dept or dept,
            effective_dept or dept,
            spend_bucket,
            "test_attribution",
            email,
            dept,
            client,
            model,
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO people(email,name,avatar,dept,raw_dept,effective_dept,"
        "spend_bucket,source)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (
            email,
            email.split("@")[0],
            "",
            effective_dept or dept,
            raw_dept or dept,
            effective_dept or dept,
            spend_bucket,
            "feishu",
        ),
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


def test_owner_leaderboard_allows_v_personnel_outsourcing_source_and_target(monkeypatch, tmp_path):
    dc = _reload(monkeypatch, tmp_path)
    source = "Keep/合作商/V/技术平台部-信息化技术部-信息化研发组"
    target = "Keep/技术平台部/信息化技术部/信息化研发组"
    conn = dc.db()
    try:
        _usage(
            dc,
            conn,
            "chenghaichao_v@keep.com",
            target,
            100,
            raw_dept=source,
            effective_dept=target,
        )
        _usage(dc, conn, "other@keep.com", "Keep/运动消费事业部", 200)
        conn.commit()

        for owner_dept in [
            "Keep/合作商/V/技术平台部-信息化技术部-信息化研发组",
            "Keep/技术平台部/信息化技术部",
            "Keep/技术平台部",
        ]:
            h = _Handler()
            dc.H._leaderboard(
                h,
                conn,
                {},
                auth_user=_role("owner@keep.com", ["department_owner"], "department", [owner_dept]),
            )
            assert h.code == 200
            assert [r["email"] for r in h.payload["leaderboard"]] == [
                "chenghaichao_v@keep.com"
            ]

        h = _Handler()
        dc.H._leaderboard(
            h,
            conn,
            {},
            auth_user=_role("unrelated@keep.com", ["department_owner"], "department",
                            ["Keep/客户服务中心"]),
        )
    finally:
        conn.close()

    assert h.code == 200
    assert h.payload["leaderboard"] == []


def test_owner_leaderboard_allows_w_business_outsourcing_source_and_target(monkeypatch, tmp_path):
    dc = _reload(monkeypatch, tmp_path)
    source = "Keep/合作商/W/深圳市奋达科技股份有限公司(SP000053)"
    target = "Keep/运动消费事业部/智能装备及运动电子交付部/软件研发部/固件组"
    conn = dc.db()
    try:
        _usage(
            dc,
            conn,
            "wb-chenliling",
            target,
            100,
            raw_dept=source,
            effective_dept=target,
            spend_bucket="business_outsourcing",
        )
        _usage(dc, conn, "other@keep.com", "Keep/客户服务中心", 200)
        conn.commit()

        for owner_dept in [
            "Keep/合作商/W/深圳市奋达科技股份有限公司(SP000053)",
            "Keep/运动消费事业部/智能装备及运动电子交付部/软件研发部",
            "Keep/运动消费事业部",
        ]:
            h = _Handler()
            dc.H._leaderboard(
                h,
                conn,
                {},
                auth_user=_role("owner@keep.com", ["department_owner"], "department", [owner_dept]),
            )
            assert h.code == 200
            assert [r["email"] for r in h.payload["leaderboard"]] == ["wb-chenliling"]

        h = _Handler()
        dc.H._leaderboard(
            h,
            conn,
            {},
            auth_user=_role("unrelated@keep.com", ["department_owner"], "department",
                            ["Keep/技术平台部"]),
        )
    finally:
        conn.close()

    assert h.code == 200
    assert h.payload["leaderboard"] == []


def test_owner_teams_returns_owned_subtree_rollup_only(monkeypatch, tmp_path):
    dc = _reload(monkeypatch, tmp_path)
    monkeypatch.setattr(
        dc,
        "_dept_headcount_map",
        lambda *_a, **_k: {
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


def test_owner_teams_allows_business_outsourcing_source_and_target(monkeypatch, tmp_path):
    dc = _reload(monkeypatch, tmp_path)
    source = "Keep/合作商/W/深圳市奋达科技股份有限公司(SP000053)"
    target = "Keep/运动消费事业部/智能装备及运动电子交付部/软件研发部/固件组"
    monkeypatch.setattr(
        dc,
        "_dept_headcount_map",
        lambda *_a, **_k: {
            target: 1,
            "Keep/客户服务中心": 1,
        },
    )
    conn = dc.db()
    try:
        _usage(
            dc,
            conn,
            "wb-chenliling",
            target,
            100,
            raw_dept=source,
            effective_dept=target,
            spend_bucket="business_outsourcing",
        )
        _usage(dc, conn, "other@keep.com", "Keep/客户服务中心", 200)
        conn.commit()

        for owner_dept in [
            source,
            "Keep/运动消费事业部/智能装备及运动电子交付部/软件研发部",
        ]:
            h = _Handler()
            dc.H._teams(
                h,
                conn,
                {},
                auth_user=_role("owner@keep.com", ["department_owner"], "department", [owner_dept]),
            )
            assert h.code == 200
            teams = {r["dept"]: r for r in h.payload["teams"]}
            assert teams["Keep"]["tokens"] == 100
            assert target in teams
            assert "Keep/客户服务中心" not in teams
    finally:
        conn.close()


def test_member_breakdown_is_filtered_to_self(monkeypatch, tmp_path):
    dc = _reload(monkeypatch, tmp_path)
    conn = dc.db()
    try:
        _usage(dc, conn, "alice@keep.com", "Keep/技术平台部/固件组", 100, "Claude Code")
        _usage(dc, conn, "bob@keep.com", "Keep/运动消费事业部/市场营销部", 200, "Cursor")
        conn.commit()
        h = _Handler()

        dc.H._breakdown(
            h,
            conn,
            {"by": ["client"]},
            auth_user=_role("alice@keep.com", ["member"], "self"),
        )
    finally:
        conn.close()

    assert h.code == 200
    assert [(r["client"], r["tokens"]) for r in h.payload["breakdown"]] == [("Claude Code", 100)]


def test_owner_breakdown_is_filtered_to_owned_subtree(monkeypatch, tmp_path):
    dc = _reload(monkeypatch, tmp_path)
    conn = dc.db()
    try:
        _usage(dc, conn, "alice@keep.com", "Keep/技术平台部/固件组", 100, "Claude Code")
        _usage(dc, conn, "peer@keep.com", "Keep/技术平台部/固件组", 50, "Cursor")
        _usage(dc, conn, "bob@keep.com", "Keep/运动消费事业部/市场营销部", 200, "Gemini CLI")
        conn.commit()
        h = _Handler()

        dc.H._breakdown(
            h,
            conn,
            {"by": ["client"]},
            auth_user=_role("lead@keep.com", ["department_owner"], "department", ["Keep/技术平台部"]),
        )
    finally:
        conn.close()

    assert h.code == 200
    assert [(r["client"], r["tokens"]) for r in h.payload["breakdown"]] == [
        ("Claude Code", 100),
        ("Cursor", 50),
    ]


def test_owner_breakdown_allows_business_outsourcing_source_owner(monkeypatch, tmp_path):
    dc = _reload(monkeypatch, tmp_path)
    source = "Keep/合作商/W/深圳市奋达科技股份有限公司(SP000053)"
    target = "Keep/运动消费事业部/智能装备及运动电子交付部/软件研发部/固件组"
    conn = dc.db()
    try:
        _usage(
            dc,
            conn,
            "wb-chenliling",
            target,
            100,
            "Claude Code",
            raw_dept=source,
            effective_dept=target,
            spend_bucket="business_outsourcing",
        )
        _usage(dc, conn, "other@keep.com", "Keep/客户服务中心", 200, "Cursor")
        conn.commit()
        h = _Handler()

        dc.H._breakdown(
            h,
            conn,
            {"by": ["client"]},
            auth_user=_role("owner@keep.com", ["department_owner"], "department", [source]),
        )
    finally:
        conn.close()

    assert h.code == 200
    assert [(r["client"], r["tokens"]) for r in h.payload["breakdown"]] == [("Claude Code", 100)]


def test_owner_client_leaderboard_allows_business_outsourcing_source_owner(monkeypatch, tmp_path):
    dc = _reload(monkeypatch, tmp_path)
    source = "Keep/合作商/W/深圳市奋达科技股份有限公司(SP000053)"
    target = "Keep/运动消费事业部/智能装备及运动电子交付部/软件研发部/固件组"
    conn = dc.db()
    try:
        _usage(
            dc,
            conn,
            "wb-chenliling",
            target,
            100,
            "Claude Code",
            raw_dept=source,
            effective_dept=target,
            spend_bucket="business_outsourcing",
        )
        _usage(dc, conn, "other@keep.com", "Keep/客户服务中心", 200, "Claude Code")
        conn.commit()
        h = _Handler()

        dc.H._leaderboard(
            h,
            conn,
            {"client": ["Claude Code"]},
            auth_user=_role("owner@keep.com", ["department_owner"], "department", [source]),
        )
    finally:
        conn.close()

    assert h.code == 200
    assert [r["email"] for r in h.payload["leaderboard"]] == ["wb-chenliling"]


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


def test_owner_ai_usage_allows_supplier_identity_without_keep_email(monkeypatch, tmp_path):
    dc = _reload(monkeypatch, tmp_path)
    source = "Keep/合作商/W/深圳市奋达科技股份有限公司(SP000053)"
    target = "Keep/运动消费事业部/智能装备及运动电子交付部/软件研发部/固件组"
    conn = dc.db()
    try:
        _usage(
            dc,
            conn,
            "wb-chenliling",
            target,
            100,
            raw_dept=source,
            effective_dept=target,
            spend_bucket="business_outsourcing",
        )
        conn.commit()
        h = _Handler()

        dc.H._ai_usage(
            h,
            conn,
            {"user": ["wb-chenliling"]},
            auth_user=_role("owner@keep.com", ["department_owner"], "department", [source]),
        )
    finally:
        conn.close()

    assert h.code == 200
    assert h.payload["user"] == "wb-chenliling"


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
