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
