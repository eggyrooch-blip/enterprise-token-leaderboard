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


def _insert_sub(conn, email, tool, tier, fee, name, dept, seat=1, synced_at="2026-06-12T10:00:00Z"):
    conn.execute(
        "INSERT OR REPLACE INTO subscriptions"
        "(email, tool, seat, tier, monthly_fee_usd, display_name, dept, synced_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (email, tool, seat, tier, fee, name, dept, synced_at),
    )


def _row_by_email(rows, email):
    for row in rows:
        if row["email"] == email:
            return row
    raise AssertionError("missing row for %s" % email)


def test_charged_counted_in_personal_cost(dc, monkeypatch, tmp_path):
    _freeze_today(monkeypatch, dc, "2026-06-12")
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    conn = dc.db()
    try:
        email = "charged@keep.com"
        _insert_people(conn, email, "Charged", "Keep/平台/基础")
        _insert_usage(dc, conn, email, "Keep/平台/基础", "2026-06-10", "litellm", "LiteLLM", 100, 3.25, 1)
        _insert_usage(dc, conn, email, "Keep/平台/基础", "2026-06-10", "cursor", "Cursor", 80, 99.0, 1)
        conn.execute("UPDATE usage SET charged=? WHERE email=? AND source='cursor'", (7.75, email))
        conn.commit()

        rows = _leaderboard(dc, conn, {"days": ["30"]})
    finally:
        conn.close()

    assert _row_by_email(rows, email)["cost"] == 11.0


def test_cost_composition_sums_to_cost(dc, monkeypatch, tmp_path):
    _freeze_today(monkeypatch, dc, "2026-06-12")
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    conn = dc.db()
    try:
        email = "mix-cost@keep.com"
        _insert_people(conn, email, "Mix Cost", "Keep/平台/基础")
        _insert_usage(dc, conn, email, "Keep/平台/基础", "2026-06-10", "litellm", "LiteLLM", 100, 2.5, 1)
        _insert_usage(dc, conn, email, "Keep/平台/基础", "2026-06-10", "api", "Hermes", 150, 4.0, 1)
        _insert_usage(dc, conn, email, "Keep/平台/基础", "2026-06-10", "subscription", "Claude Code", 80, 0.0, 1)
        _insert_usage(dc, conn, email, "Keep/平台/基础", "2026-06-10", "cursor", "Cursor", 200, 50.0, 1)
        conn.execute("UPDATE usage SET charged=? WHERE email=? AND source='cursor'", (5.5, email))
        _insert_sub(conn, email, "claude", "premium", 30.0, "Mix Cost", "Keep/平台/基础")
        conn.commit()

        row = _row_by_email(_leaderboard(dc, conn, {"days": ["30"]}), email)
    finally:
        conn.close()

    segs = row["cost_composition"]
    assert round(sum(seg["cost"] for seg in segs), 2) == round(row["cost"], 2)
    for seg in segs:
        assert {"client", "tokens", "cost", "pct"} <= set(seg)


def test_non_usage_based_charged_excluded(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    sys.modules.pop("cursor_sync", None)
    import cursor_sync  # noqa: E402

    assert cursor_sync.charged_cents({"kind": "Usage-based", "chargedCents": 500}) == 500.0
    assert cursor_sync.charged_cents({"kind": "Included in Business", "chargedCents": 500}) == 0.0
    assert cursor_sync.charged_cents({"kind": "User API Key", "chargedCents": 900}) == 0.0
