import importlib
import pathlib
import sqlite3
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import dev_collector  # noqa: E402


class _Handler:
    def _send(self, code, obj):
        self.code = code
        self.payload = obj


def _reload_dc(monkeypatch, tmp_path, excluded="sunke@keep.com"):
    monkeypatch.setenv("LEADERBOARD_EXCLUDE_EMAILS", excluded)
    dc = importlib.reload(dev_collector)
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    return dc


def _call(method, dc, conn, qs):
    handler = _Handler()
    getattr(dc.H, method)(handler, conn, qs)
    assert handler.code == 200
    return handler.payload


def _usage(dc, conn, email, dept, tokens, cost=1.0, source="subscription", client="Claude Code"):
    conn.execute(
        dc._UPSERT_SQL,
        (
            email,
            dept,
            "lifetime",
            "all",
            source,
            client,
            "",
            "model-x",
            tokens,
            0,
            0,
            0,
            0,
            tokens,
            cost,
            1,
        ),
    )


def _person(conn, email, name, dept):
    conn.execute(
        "INSERT OR REPLACE INTO people(email,name,avatar,dept) VALUES(?,?,?,?)",
        (email, name, "", dept),
    )


def test_leaderboard_excludes_configured_outlier_by_default_and_can_include_it(monkeypatch, tmp_path):
    dc = _reload_dc(monkeypatch, tmp_path)
    conn = dc.db()
    try:
        _person(conn, "sunke@keep.com", "孙可", "Keep/技术平台部/基础技术部/IT 组")
        _person(conn, "normal@keep.com", "普通用户", "Keep/技术平台部/基础技术部/安全组")
        _usage(dc, conn, "sunke@keep.com", "Keep/技术平台部/基础技术部/IT 组", 10_000, 10)
        _usage(dc, conn, "normal@keep.com", "Keep/技术平台部/基础技术部/安全组", 100, 1)
        conn.commit()

        default = _call("_leaderboard", dc, conn, {})["leaderboard"]
        included = _call("_leaderboard", dc, conn, {"include_excluded": ["1"]})["leaderboard"]
    finally:
        conn.close()

    assert [r["email"] for r in default] == ["normal@keep.com"]
    assert {r["email"] for r in included} == {"sunke@keep.com", "normal@keep.com"}


def test_teams_exclude_configured_outlier_from_rollup_by_default(monkeypatch, tmp_path):
    dc = _reload_dc(monkeypatch, tmp_path)
    monkeypatch.setattr(
        dc,
        "_dept_headcount_map",
        lambda: {
            "Keep/技术平台部/基础技术部/IT 组": 1,
            "Keep/技术平台部/基础技术部/安全组": 1,
        },
    )
    conn = dc.db()
    try:
        _person(conn, "sunke@keep.com", "孙可", "Keep/技术平台部/基础技术部/IT 组")
        _person(conn, "normal@keep.com", "普通用户", "Keep/技术平台部/基础技术部/安全组")
        _usage(dc, conn, "sunke@keep.com", "Keep/技术平台部/基础技术部/IT 组", 10_000, 10)
        _usage(dc, conn, "normal@keep.com", "Keep/技术平台部/基础技术部/安全组", 100, 1)
        conn.commit()

        default = {r["dept"]: r for r in _call("_teams", dc, conn, {})["teams"]}
        included = {
            r["dept"]: r
            for r in _call("_teams", dc, conn, {"include_excluded": ["1"]})["teams"]
        }
    finally:
        conn.close()

    assert default["Keep"]["tokens"] == 100
    assert default["Keep"]["token_people"] == 1
    assert "Keep/技术平台部/基础技术部/IT 组" not in default
    assert included["Keep"]["tokens"] == 10_100
    assert included["Keep"]["token_people"] == 2


def test_agent_owner_summary_keeps_agent_out_of_personal_tokens(monkeypatch, tmp_path):
    dc = _reload_dc(monkeypatch, tmp_path, excluded="")
    conn = dc.db()
    try:
        _person(conn, "owner@keep.com", "张三", "Keep/A/组")
        _person(conn, "agent:bot", "bot", "张三")
        _usage(dc, conn, "owner@keep.com", "Keep/A/组", 100, 1, source="subscription", client="Claude Code")
        _usage(dc, conn, "agent:bot", "", 900, 9, source="litellm_agent", client="LiteLLM")
        conn.commit()

        personal = _call("_leaderboard", dc, conn, {})["leaderboard"]
        owner_agents = _call("_agent_owner_summary", dc, conn, {"owner": ["张三"]})["agent_owner_summary"]
    finally:
        conn.close()

    assert personal == [
        {
            "email": "owner@keep.com",
            "dept": "Keep/A/组",
            "input": 100,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
            "reasoning": 0,
            "tokens": 100,
            "cost": 0,
            "messages": 1,
            "name": "张三",
            "avatar": "",
            "via": "",
            "departed": False,
            "composition": [{"client": "Claude Code", "tokens": 100, "pct": 100.0}],
            "subs": [],
        }
    ]
    assert owner_agents == [
        {
            "owner": "张三",
            "agents": 1,
            "tokens": 900,
            "cost": 9.0,
            "messages": 1,
        }
    ]

