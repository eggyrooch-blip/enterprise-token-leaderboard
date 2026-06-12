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


def test_months_overlapped_counts_distinct_calendar_months(dc):
    assert dc.months_overlapped("2026-06-01", "2026-06-30") == 1
    assert dc.months_overlapped("2026-06-30", "2026-07-01") == 2
    assert dc.months_overlapped("2026-05-31", "2026-07-01") == 3


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
    assert row["cost"] == 203.5
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

    assert before["cost"] == 81.25
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
    assert row["cost"] == 100.75
    assert row["subs"] == [{"tool": "codex", "tier": "standard", "fee": 50.0, "seats": 2}]
