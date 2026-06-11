"""Regression for the additive /v1/usage/report ingest (_upsert_hermes_usage).

Covers the codex-review hardening: within-payload summing (not overwrite),
malformed-record skipping (no zero-overwrite), and day/month/lifetime idempotency.
"""
import importlib.util
import sqlite3
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parents[1] / "collector" / "dev_collector.py"
_spec = importlib.util.spec_from_file_location("dev_collector", _MOD_PATH)
dev_collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dev_collector)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(dev_collector._CREATE_TABLE)
    return conn


def _rows(conn, email="a@c.com"):
    return {
        pt: total for pt, total in conn.execute(
            "SELECT period_type, total FROM usage WHERE source='hermes' AND email=?", (email,)
        )
    }


def test_within_payload_same_key_records_are_summed():
    conn = _conn()
    written, skipped = dev_collector._upsert_hermes_usage(
        conn, "hermes", "Hermes", "2026-06-11",
        [
            {"email": "a@c.com", "dept": "Eng", "model": "m", "input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            {"email": "a@c.com", "dept": "Eng", "model": "m", "input_tokens": 150, "output_tokens": 60, "total_tokens": 210},
        ],
    )
    assert (written, skipped) == (1, 0)
    assert _rows(conn) == {"day": 330, "month": 330, "lifetime": 330}


def test_malformed_records_are_skipped_not_zero_written():
    conn = _conn()
    # Seed a good day snapshot first.
    dev_collector._upsert_hermes_usage(
        conn, "hermes", "Hermes", "2026-06-11",
        [{"email": "a@c.com", "model": "m", "input_tokens": 100, "output_tokens": 20, "total_tokens": 120}],
    )
    # Now a batch of only-malformed records must NOT zero-overwrite the good row.
    written, skipped = dev_collector._upsert_hermes_usage(
        conn, "hermes", "Hermes", "2026-06-11",
        [
            {"email": "b@c.com", "model": "", "input_tokens": 5},      # missing model
            {"email": "", "model": "m", "input_tokens": 5},            # missing email
            {"email": "d@c.com", "model": "m", "total_tokens": 0},     # no positive tokens
            "not-a-dict",
        ],
    )
    assert written == 0 and skipped == 4
    assert _rows(conn) == {"day": 120, "month": 120, "lifetime": 120}


def test_idempotent_resend_same_day_does_not_double():
    conn = _conn()
    payload = [{"email": "a@c.com", "model": "m", "input_tokens": 100, "output_tokens": 20, "total_tokens": 120}]
    for _ in range(3):
        dev_collector._upsert_hermes_usage(conn, "hermes", "Hermes", "2026-06-11", payload)
    assert _rows(conn) == {"day": 120, "month": 120, "lifetime": 120}


def test_non_string_identity_fields_are_skipped_not_coerced():
    conn = _conn()
    # Non-string email/model are unparseable identity -> skipped (NOT coerced to "123"/"999"),
    # and must never raise (would otherwise 500 in do_POST).
    written, skipped = dev_collector._upsert_hermes_usage(
        conn, "hermes", "Hermes", "2026-06-11",
        [
            {"email": 123, "model": "m", "input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
            {"email": "a@c.com", "model": {"x": 1}, "input_tokens": 5},
            {"email": "a@c.com", "model": "m", "provider": ["x"], "input_tokens": 4, "output_tokens": 6},
        ],
    )
    assert (written, skipped) == (1, 2)               # only the 3rd record is valid
    # No garbage "123" email entered the table.
    emails = [r[0] for r in conn.execute("SELECT email FROM usage WHERE source='hermes'")]
    assert set(emails) == {"a@c.com"}
    # provider coerced to "" (non-string list ignored), not "['x']".
    provs = {r[0] for r in conn.execute("SELECT provider FROM usage WHERE source='hermes'")}
    assert provs == {""}


def test_overflow_and_nonfinite_token_values_do_not_crash():
    conn = _conn()
    # "1e10000" -> float inf -> int(inf) would OverflowError; must be treated as 0, not 500.
    written, skipped = dev_collector._upsert_hermes_usage(
        conn, "hermes", "Hermes", "2026-06-11",
        [{"email": "a@c.com", "model": "m", "input_tokens": "1e10000", "output_tokens": "nan", "total_tokens": "x"}],
    )
    # All token fields unparseable/non-finite -> total 0 -> record skipped (no positive tokens).
    assert (written, skipped) == (0, 1)
    assert dev_collector.num({"v": "1e10000"}, "v") == 0
    assert dev_collector.num({"v": "nan"}, "v") == 0


def test_empty_records_clears_existing_day_snapshot():
    conn = _conn()
    dev_collector._upsert_hermes_usage(
        conn, "hermes", "Hermes", "2026-06-11",
        [{"email": "a@c.com", "model": "m", "input_tokens": 100, "output_tokens": 20, "total_tokens": 120}],
    )
    assert _rows(conn) == {"day": 120, "month": 120, "lifetime": 120}
    # Authoritative empty snapshot (records=[]) clears the date's rows (snapshot contract).
    written, skipped = dev_collector._upsert_hermes_usage(conn, "hermes", "Hermes", "2026-06-11", [])
    assert (written, skipped) == (0, 0)
    assert _rows(conn) == {}


def test_total_falls_back_to_input_plus_output():
    conn = _conn()
    dev_collector._upsert_hermes_usage(
        conn, "hermes", "Hermes", "2026-06-11",
        [{"email": "a@c.com", "model": "m", "input_tokens": 7, "output_tokens": 8}],
    )
    assert _rows(conn)["day"] == 15


def test_delete_is_scoped_to_source_only():
    conn = _conn()
    # A foreign source row must survive a hermes upsert (no cross-source deletion).
    conn.execute(
        "INSERT INTO usage (email,dept,period_type,period,source,client,provider,model,"
        "input,output,cache_read,cache_write,reasoning,total,cost,messages) "
        "VALUES ('x@c.com','D','day','2026-06-11','litellm','LiteLLM','','m',1,1,0,0,0,2,0,0)"
    )
    dev_collector._upsert_hermes_usage(
        conn, "hermes", "Hermes", "2026-06-11",
        [{"email": "a@c.com", "model": "m", "input_tokens": 100, "output_tokens": 20, "total_tokens": 120}],
    )
    survived = conn.execute("SELECT total FROM usage WHERE source='litellm'").fetchone()
    assert survived == (2,)
