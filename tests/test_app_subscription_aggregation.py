import asyncio
import importlib.util
import pathlib
import sys
import types

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_app_module(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://collector.test/tokenleaderboard")
    fake_asyncpg = types.ModuleType("asyncpg")
    fake_asyncpg.Pool = object
    fake_asyncpg.Connection = object
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)

    spec = importlib.util.spec_from_file_location("collector_app_for_test", ROOT / "collector" / "app.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_aggregate_rows_to_email_counts_subscription_fee_once(monkeypatch):
    app = _load_app_module(monkeypatch)
    fee_fraction = (18 / 31) + (12 / 30)

    rows = [
        {
            "email": "dup@keep.com",
            "dept": "Keep/平台/基础",
            "t": 120,
            "c": 3.5,
            "api": 80,
            "sub": 40,
        },
        {
            "email": "dup@keep.com",
            "dept": "Keep/销售/华东",
            "t": 30,
            "c": 1.25,
            "api": 10,
            "sub": 20,
        },
    ]

    aggregated = app._aggregate_rows_to_email(
        rows,
        {"dup@keep.com": 25.0},
        fee_fraction,
        {"dup@keep.com": {"dept": "Keep/平台/基础"}},
    )

    assert aggregated == [
        {
            "email": "dup@keep.com",
            "dept": "Keep/平台/基础",
            "total_tokens": 150,
            "gateway_cost": 4.75,
            "api_tokens": 90,
            "subscription_tokens": 60,
            "cost_usd": round(4.75 + 25.0 * fee_fraction, 4),
        }
    ]


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class _FakeConn:
    def __init__(self, subs_rows, usage_rows, dept_rows=None, tool_rows=None, code_rows=None):
        self._subs_rows = subs_rows
        self._usage_rows = usage_rows
        self._dept_rows = dept_rows or []
        self._tool_rows = tool_rows or []
        self._code_rows = code_rows or []

    async def fetch(self, sql, *args):
        if "FROM subscriptions" in sql:
            return self._subs_rows
        if "FROM usage_daily" in sql and "GROUP BY email, dept" in sql:
            return self._usage_rows
        if "FROM usage_daily" in sql and "GROUP BY dept" in sql:
            return self._dept_rows
        if "FROM usage_daily" in sql and "GROUP BY tool" in sql:
            return self._tool_rows
        if "FROM code_daily" in sql:
            return self._code_rows
        raise AssertionError(sql)


def _freeze_today(monkeypatch, app, today_text):
    real_date = app.date
    today = real_date.fromisoformat(today_text)

    class _FakeDate(real_date):
        @classmethod
        def today(cls):
            return today

    monkeypatch.setattr(app, "date", _FakeDate)


def test_prorated_month_fraction_examples(monkeypatch):
    app = _load_app_module(monkeypatch)

    assert app.prorated_month_fraction("2026-06-06", "2026-06-12") == pytest.approx(7 / 30)
    assert app.prorated_month_fraction("2026-05-14", "2026-06-12") == pytest.approx((18 / 31) + (12 / 30))
    assert app.prorated_month_fraction("2026-06-01", "2026-06-30") == 1.0
    assert app.prorated_month_fraction("2026-06-12", "2026-06-11") == 1.0


def test_subscription_fraction_uses_days_window(monkeypatch):
    app = _load_app_module(monkeypatch)
    _freeze_today(monkeypatch, app, "2026-06-12")

    assert app._subscription_fraction(7) == pytest.approx(7 / 30)
    assert app._subscription_fraction(30) == pytest.approx((18 / 31) + (12 / 30))
    assert app._subscription_fraction(30 + 1) != 1.0


def test_leaderboard_and_dashboard_skip_roster_only_subscription_rows(monkeypatch):
    app = _load_app_module(monkeypatch)
    _freeze_today(monkeypatch, app, "2026-06-12")
    conn = _FakeConn(
        subs_rows=[
            {
                "email": "active@keep.com",
                "tool": "claude",
                "tier": "premium",
                "monthly_fee_usd": 50.0,
                "seats": 1,
                "display_name": "Active",
                "dept": "Keep/平台/基础",
            },
            {
                "email": "idle@keep.com",
                "tool": "codex",
                "tier": "standard",
                "monthly_fee_usd": 25.0,
                "seats": 1,
                "display_name": "Idle",
                "dept": "Keep/平台/基础",
            },
        ],
        usage_rows=[
            {
                "email": "active@keep.com",
                "dept": "Keep/平台/基础",
                "total_tokens": 120,
                "gateway_cost": 1.5,
                "t": 120,
                "c": 1.5,
                "api": 120,
                "sub": 0,
            }
        ],
    )
    monkeypatch.setattr(app, "_pool", _FakePool(conn))

    leaderboard = asyncio.run(app.leaderboard(days=30, source="all", limit=100))
    assert leaderboard["ranking"] == [
        {
            "email": "active@keep.com",
            "dept": "Keep/平台/基础",
            "total_tokens": 120,
            "cost_usd": 50.5323,
            "subs": [{"tool": "claude", "tier": "premium", "fee": 50.0, "seats": 1}],
        }
    ]

    html = asyncio.run(app.dashboard(days=30))
    assert "active@keep.com" in html
    assert "idle@keep.com" not in html
