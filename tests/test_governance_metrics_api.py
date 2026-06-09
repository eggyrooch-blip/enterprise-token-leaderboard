import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import dev_collector  # noqa: E402


REQUIRED_METRIC_IDS = {
    "cost_efficiency",
    "adoption_coverage",
    "code_acceptance",
    "delivery_quality",
    "reliability_budget",
    "privacy_purpose",
    "collection_health",
}


class _DummyHandler:
    def _send(self, code, obj):
        self.code = code
        self.payload = obj


def _insert_usage(conn, email, dept, period_type, period, source, client,
                  tokens, cost, messages, cache_read=0, cache_write=0):
    conn.execute(dev_collector._UPSERT_SQL, (
        email, dept, period_type, period, source, client, "", "model-x",
        10, 5, cache_read, cache_write, 0, tokens, cost, messages,
    ))


def test_governance_metrics_api_computes_available_metrics(monkeypatch, tmp_path):
    monkeypatch.setattr(dev_collector, "DB", str(tmp_path / "tok.db"))
    conn = dev_collector.db()
    try:
        _insert_usage(conn, "a@example.com", "Eng/Platform", "lifetime", "all",
                      "subscription", "Claude Code", 500, 1.0, 10, cache_read=300, cache_write=50)
        _insert_usage(conn, "b@example.com", "Eng/App", "lifetime", "all",
                      "cursor", "Cursor", 500, 2.0, 20, cache_read=200, cache_write=20)
        _insert_usage(conn, "agent:review", "Eng/Platform", "lifetime", "all",
                      "litellm_agent", "LiteLLM", 70, 0.5, 7)
        _insert_usage(conn, "a@example.com", "Eng/Platform", "day", "2026-06-08",
                      "subscription", "Claude Code", 100, 0.2, 1)
        _insert_usage(conn, "b@example.com", "Eng/App", "day", "2026-06-02",
                      "cursor", "Cursor", 90, 0.3, 2)
        _insert_usage(conn, "agent:review", "Eng/Platform", "day", "2026-06-08",
                      "litellm_agent", "LiteLLM", 80, 0.4, 3)
        conn.execute(
            "INSERT OR REPLACE INTO report_log(serial,email,hostname,ip,via,reported_at) "
            "VALUES (?,?,?,?,?,?)",
            ("SER-1", "a@example.com", "mac-a", "127.0.0.1", "mdm", "2026-06-08T10:00:00"),
        )
        conn.commit()

        handler = _DummyHandler()
        dev_collector.H._governance_metrics(handler, conn)
    finally:
        conn.close()

    assert handler.code == 200
    payload = handler.payload
    metrics = {m["id"]: m for m in payload["metrics"]}

    assert set(metrics) == REQUIRED_METRIC_IDS
    assert metrics["cost_efficiency"]["availability"] == "computed"
    assert metrics["adoption_coverage"]["availability"] == "computed"
    assert metrics["code_acceptance"]["availability"] == "partial"
    assert metrics["delivery_quality"]["availability"] == "pending"
    assert metrics["privacy_purpose"]["availability"] == "computed"

    assert payload["summary"]["lifetime"]["users"] == 3
    assert payload["summary"]["lifetime"]["depts"] == 2
    assert payload["summary"]["lifetime"]["clients"] == 3
    assert payload["summary"]["lifetime"]["tokens"] == 1070
    assert payload["summary"]["day"]["max_date"] == "2026-06-08"
    assert payload["summary"]["last7"]["users"] == 3
    assert payload["summary"]["report_log"]["reports"] == 1
