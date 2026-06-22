import importlib
import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import dev_collector  # noqa: E402


class _DummyHandler(object):
    def _send(self, code, obj):
        self.code = code
        self.payload = obj


def _reload_dc(monkeypatch, **env):
    for key in ("FEISHU_PACKAGE_CNY", "FEISHU_PACKAGE_POINTS", "CNY_PER_USD"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return importlib.reload(dev_collector)


def _leaderboard(dc, conn, qs):
    handler = _DummyHandler()
    dc.H._leaderboard(handler, conn, qs)
    assert handler.code == 200
    return handler.payload["leaderboard"]


def _feishu(dc, conn, qs):
    handler = _DummyHandler()
    dc.H._feishu(handler, conn, qs)
    assert handler.code == 200
    return handler.payload


def _row_by_email(rows, email):
    for row in rows:
        if row["email"] == email:
            return row
    raise AssertionError("missing row for %s" % email)


def _insert_people(conn, email, name, dept):
    conn.execute(
        "INSERT OR REPLACE INTO people(email, name, avatar, dept) VALUES(?,?,?,?)",
        (email, name, "", dept),
    )


def _insert_usage(dc, conn, email, tokens, cost):
    conn.execute(dc._UPSERT_SQL, (
        email, "Keep/平台/基础", "day", "2026-06-10", "api", "Hermes", "", "model-x",
        tokens, 0, 0, 0, 0, tokens, cost, 1,
    ))


def _insert_feishu(conn, email, credits):
    conn.execute(
        "INSERT INTO feishu_member"
        "(email, name, dept, feature_key, credits, usage_date, avatar, entity_id)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (email, email.split("@")[0], "Keep/平台/基础", "AI_credits", credits, "2026-06-10", "", ""),
    )


def test_feishu_credits_recorded_separately_not_merged_into_main_board(monkeypatch, tmp_path):
    # 2026-06-22 去点改造后的契约:飞书「点」(credits)不再并入主榜 token/cost,
    # 只作附记字段(feishu_credits/feishu_cost);纯飞书用户不出现在主 token 榜(归飞书 tab)。
    dc = _reload_dc(monkeypatch)
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    conn = dc.db()
    try:
        _insert_people(conn, "with-feishu@keep.com", "With Feishu", "Keep/平台/基础")
        _insert_people(conn, "plain@keep.com", "Plain", "Keep/平台/基础")
        _insert_usage(dc, conn, "with-feishu@keep.com", 100, 1.25)
        _insert_usage(dc, conn, "plain@keep.com", 80, 2.5)
        _insert_feishu(conn, "with-feishu@keep.com", 1000)
        _insert_feishu(conn, "feishu-only@keep.com", 500)
        conn.commit()

        rows = _leaderboard(dc, conn, {"from": ["2026-06-01"], "to": ["2026-06-30"]})
    finally:
        conn.close()

    rate = dc.FEISHU_USD_PER_POINT
    with_feishu = _row_by_email(rows, "with-feishu@keep.com")
    # token 与 cost 是纯 token 口径,不含 credits 折算
    assert with_feishu["tokens"] == 100
    assert with_feishu["cost"] == 1.25
    # credits 仅作附记字段
    assert with_feishu["feishu_credits"] == 1000
    assert with_feishu["feishu_cost"] == round(1000 * rate, 4)

    # 纯飞书用户(无 token 用量)不在主榜
    with pytest.raises(AssertionError):
        _row_by_email(rows, "feishu-only@keep.com")

    plain = _row_by_email(rows, "plain@keep.com")
    assert plain["cost"] == 2.5
    assert "feishu_cost" not in plain


def test_feishu_rate_comes_from_env_and_is_exposed_on_feishu_payload(monkeypatch, tmp_path):
    dc = _reload_dc(monkeypatch, CNY_PER_USD="10")
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    conn = dc.db()
    try:
        _insert_feishu(conn, "rate@keep.com", 1000)
        conn.execute(
            "INSERT INTO feishu_quota(feature_key, quota, used, remain, period_start, period_end)"
            " VALUES (?,?,?,?,?,?)",
            ("AI_credits", 2000000, 1000, 1999000, "2026-06-01", "2026-06-30"),
        )
        conn.commit()

        rows = _leaderboard(dc, conn, {"from": ["2026-06-01"], "to": ["2026-06-30"]})
        payload = _feishu(dc, conn, {"from": ["2026-06-01"], "to": ["2026-06-30"]})
    finally:
        conn.close()

    assert dc.FEISHU_USD_PER_POINT == pytest.approx(99000 / 2000000 / 10)
    # 纯飞书用户不在主 token 榜;费率改为在飞书 tab 暴露
    with pytest.raises(AssertionError):
        _row_by_email(rows, "rate@keep.com")
    assert payload["usd_per_point"] == pytest.approx(0.00495)
    assert payload["package_cny"] == 99000
    assert payload["package_points"] == 2000000
    assert payload["cny_per_usd"] == 10
    assert payload["package_usd"] == pytest.approx(9900)
