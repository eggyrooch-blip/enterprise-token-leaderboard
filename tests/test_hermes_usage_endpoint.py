"""Regression for the additive /v1/usage/report ingest (_upsert_hermes_usage).

Covers the codex-review hardening: within-payload summing (not overwrite),
malformed-record skipping (no zero-overwrite), and day/month/lifetime idempotency.
"""
import importlib.util
import json
import sqlite3
import sys
import threading
import types
import urllib.request
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parents[1] / "collector" / "dev_collector.py"
_spec = importlib.util.spec_from_file_location("dev_collector", _MOD_PATH)
dev_collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dev_collector)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(dev_collector._CREATE_TABLE)
    conn.execute("CREATE TABLE IF NOT EXISTS people(email TEXT PRIMARY KEY, name TEXT, avatar TEXT, dept TEXT)")
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


def test_hermes_usage_autofills_people_from_feilian(monkeypatch):
    class FakeFC:
        def root_department_id(self):
            return "root"

        def user_by_email(self, email, root):
            assert root == "root"
            if email == "fresh@example.com":
                return {
                    "full_name": "新用户",
                    "avatar": "https://avatar.example/fresh.png",
                    "department_path": "Keep/客户服务中心/客服运营部/运营支持组",
                }
            return None

    fake_mod = types.ModuleType("feilian_client")
    fake_mod.FeilianClient = lambda: FakeFC()
    monkeypatch.setitem(sys.modules, "feilian_client", fake_mod)
    monkeypatch.setattr(dev_collector, "_fc", None)

    conn = _conn()
    payload = [
        {
            "email": "fresh@example.com",
            "dept": "unknown",
            "model": "m",
            "input_tokens": 7,
            "output_tokens": 8,
        }
    ]
    dev_collector._upsert_hermes_usage(conn, "hermes", "Hermes", "2026-06-12", payload)
    filled = dev_collector._autofill_people_for_emails(conn, ["fresh@example.com"])

    assert filled == 1
    assert conn.execute("SELECT name,dept FROM people WHERE email='fresh@example.com'").fetchone() == (
        "新用户",
        "Keep/客户服务中心/客服运营部/运营支持组",
    )


def test_hermes_people_autofill_is_best_effort_when_feilian_fails(monkeypatch):
    class BrokenFC:
        def root_department_id(self):
            raise RuntimeError("feilian unavailable")

    fake_mod = types.ModuleType("feilian_client")
    fake_mod.FeilianClient = lambda: BrokenFC()
    monkeypatch.setitem(sys.modules, "feilian_client", fake_mod)
    monkeypatch.setattr(dev_collector, "_fc", None)

    conn = _conn()
    filled = dev_collector._autofill_people_for_emails(conn, ["fresh@example.com"])

    assert filled == 0
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone() == (0,)


def test_hermes_people_autofill_preserves_existing_complete_people_row(monkeypatch):
    class FakeFC:
        def root_department_id(self):
            return "root"

        def user_by_email(self, email, root):
            return {
                "full_name": "错误覆盖",
                "avatar": "https://avatar.example/wrong.png",
                "department_path": "Keep/错误部门",
            }

    fake_mod = types.ModuleType("feilian_client")
    fake_mod.FeilianClient = lambda: FakeFC()
    monkeypatch.setitem(sys.modules, "feilian_client", fake_mod)
    monkeypatch.setattr(dev_collector, "_fc", None)

    conn = _conn()
    conn.execute(
        "INSERT INTO people(email,name,avatar,dept) VALUES(?,?,?,?)",
        ("known@example.com", "已知用户", "https://avatar.example/known.png", "Keep/技术平台部/基础技术部/IT 组"),
    )
    filled = dev_collector._autofill_people_for_emails(conn, ["known@example.com"])

    assert filled == 0
    assert conn.execute("SELECT name,dept FROM people WHERE email='known@example.com'").fetchone() == (
        "已知用户",
        "Keep/技术平台部/基础技术部/IT 组",
    )


def test_hermes_autofill_scans_existing_usage_missing_people(monkeypatch):
    class FakeFC:
        def root_department_id(self):
            return "root"

        def user_by_email(self, email, root):
            assert root == "root"
            data = {
                "old@example.com": ("历史用户", "Keep/CFO 线/法务部"),
                "fresh@example.com": ("新用户", "Keep/客户服务中心/客服运营部"),
            }
            if email not in data:
                return None
            name, dept = data[email]
            return {"full_name": name, "avatar": "https://avatar.example/%s.png" % name, "department_path": dept}

    fake_mod = types.ModuleType("feilian_client")
    fake_mod.FeilianClient = lambda: FakeFC()
    monkeypatch.setitem(sys.modules, "feilian_client", fake_mod)
    monkeypatch.setattr(dev_collector, "_fc", None)

    conn = _conn()
    dev_collector._upsert_hermes_usage(
        conn,
        "hermes",
        "Hermes",
        "2026-06-11",
        [{"email": "old@example.com", "model": "m", "input_tokens": 10, "output_tokens": 1}],
    )

    filled = dev_collector._autofill_people_for_hermes_usage(
        conn,
        "hermes",
        "Hermes",
        [{"email": "fresh@example.com", "model": "m", "input_tokens": 7, "output_tokens": 8}],
    )

    assert filled == 2
    assert conn.execute("SELECT name,dept FROM people WHERE email='old@example.com'").fetchone() == (
        "历史用户",
        "Keep/CFO 线/法务部",
    )
    assert conn.execute("SELECT name,dept FROM people WHERE email='fresh@example.com'").fetchone() == (
        "新用户",
        "Keep/客户服务中心/客服运营部",
    )


def test_usage_report_commits_usage_before_people_autofill(monkeypatch, tmp_path):
    db_path = tmp_path / "tok.db"
    monkeypatch.setattr(dev_collector, "DB", str(db_path))
    monkeypatch.setattr(dev_collector, "TOKENS", {"devtoken"})
    observed = []

    def observe_committed_usage(_conn, _source, _client, _records):
        with sqlite3.connect(str(db_path)) as probe:
            observed.append(
                probe.execute(
                    "SELECT total FROM usage WHERE email=? AND client=? AND period_type=?",
                    ("fresh@example.com", "Hermes", "day"),
                ).fetchone()
            )
        return 0

    monkeypatch.setattr(dev_collector, "_autofill_people_for_hermes_usage", observe_committed_usage)
    server = dev_collector.ThreadingHTTPServer(("127.0.0.1", 0), dev_collector.H)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        payload = {
            "source": "hermes",
            "client": "Hermes",
            "date": "2026-06-12",
            "records": [
                {"email": "fresh@example.com", "model": "m", "input_tokens": 7, "output_tokens": 8}
            ],
        }
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/usage/report" % server.server_address[1],
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer devtoken", "Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
    finally:
        server.shutdown()
        thread.join(timeout=3)

    assert observed == [(15,)]
