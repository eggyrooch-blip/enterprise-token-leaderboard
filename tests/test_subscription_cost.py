import datetime
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


@pytest.fixture()
def dc():
    return importlib.reload(dev_collector)


def _freeze_today(monkeypatch, dc, today_text):
    real_date = datetime.date
    today = datetime.datetime.strptime(today_text, "%Y-%m-%d").date()

    class _FakeDate(real_date):
        @classmethod
        def today(cls):
            return today

    monkeypatch.setattr(dc.datetime, "date", _FakeDate)
    return today


def _leaderboard(dc, conn, qs):
    handler = _DummyHandler()
    dc.H._leaderboard(handler, conn, qs)
    assert handler.code == 200
    return handler.payload["leaderboard"]


def _cursor_rows(dc, conn, qs):
    handler = _DummyHandler()
    dc.H._cursor(handler, conn, qs)
    assert handler.code == 200
    return handler.payload["cursor"]


def _governance(dc, conn):
    handler = _DummyHandler()
    dc.H._governance_metrics(handler, conn)
    assert handler.code == 200
    return handler.payload


def _insert_usage(dc, conn, email, dept, period, source, client, tokens, cost, messages):
    conn.execute(dc._UPSERT_SQL, (
        email, dept, "day", period, source, client, "", "model-x",
        tokens, 0, 0, 0, 0, tokens, cost, messages,
    ))


def _insert_people(conn, email, name, dept, avatar=""):
    conn.execute(
        "INSERT OR REPLACE INTO people(email, name, avatar, dept) VALUES(?,?,?,?)",
        (email, name, avatar, dept),
    )


def _insert_sub(conn, email, tool, tier, fee, name, dept, seats=1, synced_at="2026-06-12T10:00:00Z"):
    conn.execute(
        "INSERT OR REPLACE INTO subscriptions"
        "(email, tool, tier, monthly_fee_usd, seats, display_name, dept, synced_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (email, tool, tier, fee, seats, name, dept, synced_at),
    )


def _row_by_email(rows, email):
    for row in rows:
        if row["email"] == email:
            return row
    raise AssertionError("missing row for %s" % email)


def _idle_subscription(payload):
    return payload["summary"]["subscriptions"]["idle"]


def test_prorated_month_fraction_examples(dc):
    assert dc.prorated_month_fraction("2026-06-06", "2026-06-12") == pytest.approx(7 / 30)
    assert dc.prorated_month_fraction("2026-05-14", "2026-06-12") == pytest.approx((18 / 31) + (12 / 30))
    assert dc.prorated_month_fraction("2026-06-01", "2026-06-30") == 1.0
    assert dc.prorated_month_fraction("2026-06-12", "2026-06-11") == 1.0


def test_idle_subscription_person_is_absent_from_leaderboard_and_exposed_in_governance(dc, monkeypatch, tmp_path):
    _freeze_today(monkeypatch, dc, "2026-06-12")
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    conn = dc.db()
    try:
        _insert_people(conn, "solo@keep.com", "Solo", "Keep/平台/基础")
        _insert_sub(conn, "solo@keep.com", "codex", "standard", 25.0, "Solo", "Keep/平台/基础")
        _insert_sub(conn, "solo@keep.com", "cursor", "standard", 40.0, "Solo", "Keep/平台/基础")
        conn.commit()

        rows = _leaderboard(dc, conn, {"days": ["30"]})
        payload = _governance(dc, conn)
    finally:
        conn.close()

    assert not [row for row in rows if row["email"] == "solo@keep.com"]
    assert _idle_subscription(payload) == {
        "count": 1,
        "monthly_fee_usd": 65.0,
        "people": [
            {"email": "solo@keep.com", "tool": "codex", "fee": 25.0},
            {"email": "solo@keep.com", "tool": "cursor", "fee": 40.0},
        ],
    }


def test_person_cost_uses_gateway_actual_plus_subscription_fee(dc, monkeypatch, tmp_path):
    _freeze_today(monkeypatch, dc, "2026-06-12")
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    conn = dc.db()
    try:
        _insert_people(conn, "mix@keep.com", "Mix", "Keep/平台/基础")
        _insert_sub(conn, "mix@keep.com", "claude", "premium", 100.0, "Mix", "Keep/平台/基础")
        _insert_usage(dc, conn, "mix@keep.com", "Keep/平台/基础", "2026-06-10", "litellm", "LiteLLM", 120, 3.5, 2)
        _insert_usage(dc, conn, "mix@keep.com", "Keep/平台/基础", "2026-06-10", "subscription", "Claude Code", 80, 99.0, 1)
        conn.commit()

        rows = _leaderboard(dc, conn, {"days": ["30"]})
        payload = _governance(dc, conn)
    finally:
        conn.close()

    row = _row_by_email(rows, "mix@keep.com")
    assert row["tokens"] == 200
    assert row["cost"] == 101.5645
    assert row["subs"] == [{"tool": "claude", "tier": "premium", "fee": 100.0, "seats": 1}]
    assert _idle_subscription(payload) == {"count": 0, "monthly_fee_usd": 0.0, "people": []}


def test_subscription_removal_drops_fee_and_badges(dc, monkeypatch, tmp_path):
    _freeze_today(monkeypatch, dc, "2026-06-12")
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    conn = dc.db()
    try:
        _insert_people(conn, "gone@keep.com", "Gone", "Keep/平台/基础")
        _insert_sub(conn, "gone@keep.com", "cursor", "standard", 40.0, "Gone", "Keep/平台/基础")
        _insert_usage(dc, conn, "gone@keep.com", "Keep/平台/基础", "2026-06-10", "api", "Hermes", 90, 1.25, 1)
        conn.commit()

        before = _row_by_email(_leaderboard(dc, conn, {"days": ["30"]}), "gone@keep.com")
        conn.execute("DELETE FROM subscriptions WHERE email=?", ("gone@keep.com",))
        conn.commit()
        after = _row_by_email(_leaderboard(dc, conn, {"days": ["30"]}), "gone@keep.com")
    finally:
        conn.close()

    # days=30 @ today=2026-06-12 → window 2026-05-14..2026-06-12 → 摊销倍数 18/31 + 12/30。
    assert before["cost"] == round(1.25 + 40.0 * ((18 / 31) + (12 / 30)), 4)
    assert before["subs"] == [{"tool": "cursor", "tier": "standard", "fee": 40.0, "seats": 1}]
    assert after["cost"] == 1.25
    assert after["subs"] == []


def test_governance_metrics_exposes_subscription_unresolved_count(dc, monkeypatch, tmp_path):
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    conn = dc.db()
    try:
        conn.execute(
            "INSERT INTO subscriptions_unresolved"
            "(tool, display_name, raw_email, dept, reason, synced_at)"
            " VALUES (?,?,?,?,?,?)",
            ("codex", "Ghost", "ghost@gmail.com", "Keep/平台/基础", "no_match", "2026-06-12T10:00:00Z"),
        )
        conn.commit()

        payload = _governance(dc, conn)
    finally:
        conn.close()

    assert payload["summary"]["subscriptions"] == {
        "unresolved": 1,
        "idle": {"count": 0, "monthly_fee_usd": 0.0, "people": []},
    }


def test_subscription_cost_uses_aggregated_fee_row_and_preserves_seats(dc, monkeypatch, tmp_path):
    _freeze_today(monkeypatch, dc, "2026-06-12")
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    conn = dc.db()
    try:
        _insert_people(conn, "seats@keep.com", "Seats", "Keep/平台/基础")
        _insert_sub(conn, "seats@keep.com", "codex", "standard", 50.0, "Seats", "Keep/平台/基础", seats=2)
        _insert_usage(dc, conn, "seats@keep.com", "Keep/平台/基础", "2026-06-10", "api", "Hermes", 20, 0.75, 1)
        conn.commit()

        rows = _leaderboard(dc, conn, {"days": ["30"]})
    finally:
        conn.close()

    row = _row_by_email(rows, "seats@keep.com")
    assert row["tokens"] == 20
    assert row["cost"] == 49.7823
    assert row["subs"] == [{"tool": "codex", "tier": "standard", "fee": 50.0, "seats": 2}]


def test_subscription_cost_prorates_fee_inside_one_month_and_keeps_stored_seat_multiplier(dc, monkeypatch, tmp_path):
    _freeze_today(monkeypatch, dc, "2026-06-12")
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    conn = dc.db()
    try:
        _insert_people(conn, "premium@keep.com", "Premium", "Keep/平台/基础")
        _insert_sub(conn, "premium@keep.com", "claude", "premium", 240.0, "Premium", "Keep/平台/基础", seats=3)
        _insert_usage(dc, conn, "premium@keep.com", "Keep/平台/基础", "2026-06-06", "api", "Hermes", 10, 0.5, 1)
        conn.commit()

        rows = _leaderboard(dc, conn, {"days": ["7"]})
    finally:
        conn.close()

    row = _row_by_email(rows, "premium@keep.com")
    assert row["cost"] == round(0.5 + 240.0 * (7 / 30), 4)
    assert row["subs"] == [{"tool": "claude", "tier": "premium", "fee": 240.0, "seats": 3}]


def test_cursor_board_attaches_only_cursor_badge_and_keeps_gateway_cost(dc, monkeypatch, tmp_path):
    _freeze_today(monkeypatch, dc, "2026-06-12")
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    conn = dc.db()
    try:
        _insert_people(conn, "cursor-sub@keep.com", "Cursor Sub", "Keep/平台/基础")
        _insert_people(conn, "cursor-none@keep.com", "Cursor None", "Keep/平台/基础")
        _insert_people(conn, "cursor-leak@keep.com", "Cursor Leak", "Keep/平台/基础")
        _insert_sub(conn, "cursor-sub@keep.com", "cursor", "standard", 40.0, "Cursor Sub", "Keep/平台/基础")
        _insert_sub(conn, "cursor-sub@keep.com", "claude", "premium", 120.0, "Cursor Sub", "Keep/平台/基础")
        _insert_sub(conn, "cursor-leak@keep.com", "claude", "premium", 120.0, "Cursor Leak", "Keep/平台/基础")
        _insert_usage(dc, conn, "cursor-sub@keep.com", "Keep/平台/基础", "2026-06-10", "cursor", "Cursor", 150, 4.2, 7)
        _insert_usage(dc, conn, "cursor-none@keep.com", "Keep/平台/基础", "2026-06-10", "cursor", "Cursor", 120, 1.8, 5)
        _insert_usage(dc, conn, "cursor-leak@keep.com", "Keep/平台/基础", "2026-06-10", "cursor", "Cursor", 90, 2.6, 4)
        conn.commit()

        rows = _cursor_rows(dc, conn, {"days": ["30"]})
    finally:
        conn.close()

    subbed = _row_by_email(rows, "cursor-sub@keep.com")
    assert subbed["cost"] == 4.2
    assert subbed["subs"] == [{"tool": "cursor", "tier": "standard", "fee": 40.0, "seats": 1}]
    assert _row_by_email(rows, "cursor-none@keep.com")["subs"] == []
    assert _row_by_email(rows, "cursor-leak@keep.com")["subs"] == []


def test_claude_board_attaches_only_claude_badge_and_keeps_gateway_cost(dc, monkeypatch, tmp_path):
    _freeze_today(monkeypatch, dc, "2026-06-12")
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    conn = dc.db()
    try:
        _insert_people(conn, "claude-sub@keep.com", "Claude Sub", "Keep/平台/基础")
        _insert_people(conn, "claude-none@keep.com", "Claude None", "Keep/平台/基础")
        _insert_people(conn, "claude-leak@keep.com", "Claude Leak", "Keep/平台/基础")
        _insert_sub(conn, "claude-sub@keep.com", "claude", "premium", 120.0, "Claude Sub", "Keep/平台/基础")
        _insert_sub(conn, "claude-sub@keep.com", "cursor", "standard", 40.0, "Claude Sub", "Keep/平台/基础")
        _insert_sub(conn, "claude-leak@keep.com", "codex", "standard", 30.0, "Claude Leak", "Keep/平台/基础")
        _insert_usage(dc, conn, "claude-sub@keep.com", "Keep/平台/基础", "2026-06-10", "subscription", "Claude Code", 210, 9.5, 3)
        _insert_usage(dc, conn, "claude-none@keep.com", "Keep/平台/基础", "2026-06-10", "subscription", "Claude Code", 120, 2.0, 2)
        _insert_usage(dc, conn, "claude-leak@keep.com", "Keep/平台/基础", "2026-06-10", "subscription", "Claude Code", 100, 1.2, 1)
        conn.commit()

        rows = _leaderboard(dc, conn, {"client": ["Claude Code"], "days": ["30"]})
        person_rows = _leaderboard(dc, conn, {"days": ["30"]})
    finally:
        conn.close()

    subbed = _row_by_email(rows, "claude-sub@keep.com")
    assert subbed["cost"] == 0
    assert subbed["subs"] == [{"tool": "claude", "tier": "premium", "fee": 120.0, "seats": 1}]
    assert _row_by_email(person_rows, "claude-sub@keep.com")["cost"] == round((120.0 + 40.0) * ((18 / 31) + (12 / 30)), 4)
    assert _row_by_email(rows, "claude-none@keep.com")["subs"] == []
    assert _row_by_email(rows, "claude-leak@keep.com")["subs"] == []


def test_codex_board_attaches_only_codex_badge_and_keeps_gateway_cost(dc, monkeypatch, tmp_path):
    _freeze_today(monkeypatch, dc, "2026-06-12")
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    conn = dc.db()
    try:
        _insert_people(conn, "codex-sub@keep.com", "Codex Sub", "Keep/平台/基础")
        _insert_people(conn, "codex-none@keep.com", "Codex None", "Keep/平台/基础")
        _insert_people(conn, "codex-leak@keep.com", "Codex Leak", "Keep/平台/基础")
        _insert_sub(conn, "codex-sub@keep.com", "codex", "standard", 30.0, "Codex Sub", "Keep/平台/基础")
        _insert_sub(conn, "codex-sub@keep.com", "claude", "premium", 120.0, "Codex Sub", "Keep/平台/基础")
        _insert_sub(conn, "codex-leak@keep.com", "claude", "premium", 120.0, "Codex Leak", "Keep/平台/基础")
        _insert_sub(conn, "codex-leak@keep.com", "cursor", "standard", 40.0, "Codex Leak", "Keep/平台/基础")
        _insert_usage(dc, conn, "codex-sub@keep.com", "Keep/平台/基础", "2026-06-10", "subscription", "Codex CLI", 180, 3.4, 2)
        _insert_usage(dc, conn, "codex-none@keep.com", "Keep/平台/基础", "2026-06-10", "subscription", "Codex CLI", 130, 1.1, 1)
        _insert_usage(dc, conn, "codex-leak@keep.com", "Keep/平台/基础", "2026-06-10", "subscription", "Codex CLI", 110, 0.9, 1)
        conn.commit()

        rows = _leaderboard(dc, conn, {"client": ["Codex CLI"], "days": ["30"]})
        person_rows = _leaderboard(dc, conn, {"days": ["30"]})
    finally:
        conn.close()

    subbed = _row_by_email(rows, "codex-sub@keep.com")
    assert subbed["cost"] == 0
    assert subbed["subs"] == [{"tool": "codex", "tier": "standard", "fee": 30.0, "seats": 1}]
    assert _row_by_email(person_rows, "codex-sub@keep.com")["cost"] == round((30.0 + 120.0) * ((18 / 31) + (12 / 30)), 4)
    assert _row_by_email(rows, "codex-none@keep.com")["subs"] == []
    assert _row_by_email(rows, "codex-leak@keep.com")["subs"] == []
