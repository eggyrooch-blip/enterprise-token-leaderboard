import importlib
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
    "feishu_directory_sync_health",
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
        dev_collector._state_set(conn, "feishu_directory_sync_status", "success")
        dev_collector._state_set(conn, "feishu_directory_sync_last_success", "2026-06-18T10:00:00Z")
        dev_collector._state_set(conn, "feishu_directory_sync_visibility_warnings", "[]")
        dev_collector._state_set(conn, "feishu_directory_sync_production_enablement_blocked", "1")
        dev_collector._state_set(conn, "feishu_directory_sync_business_rollup_enabled", "0")
        dev_collector._state_set(conn, "feishu_directory_sync_resolved_business_outsourcing_rate", "0.3333")
        dev_collector._state_set(conn, "feishu_directory_sync_min_required_rate", "0.8")
        conn.commit()

        handler = _DummyHandler()
        dev_collector.H._governance_metrics(handler, conn, {})
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
    assert metrics["feishu_directory_sync_health"]["availability"] == "partial"
    assert metrics["privacy_purpose"]["availability"] == "computed"

    assert payload["summary"]["lifetime"]["users"] == 3
    assert payload["summary"]["lifetime"]["depts"] == 2
    assert payload["summary"]["lifetime"]["clients"] == 3
    assert payload["summary"]["lifetime"]["tokens"] == 1070
    assert payload["summary"]["day"]["max_date"] == "2026-06-08"
    assert payload["summary"]["last7"]["users"] == 3
    assert payload["summary"]["report_log"]["reports"] == 1
    assert payload["summary"]["feishu_directory_sync"] == {
        "status": "success",
        "last_success": "2026-06-18T10:00:00Z",
        "last_attempt": "",
        "last_error": "",
        "visibility_warnings": [],
        "visibility_warnings_count": 0,
        "production_enablement_blocked": True,
        "business_rollup_enabled": False,
        "resolved_business_outsourcing_rate": 0.3333,
        "min_required_rate": 0.8,
        "users": 0,
        "departments": 0,
        "supplier_departments": 0,
        "unresolved": 0,
    }


def test_meta_exposes_feishu_directory_sync_health(monkeypatch, tmp_path):
    monkeypatch.setattr(dev_collector, "DB", str(tmp_path / "tok.db"))
    conn = dev_collector.db()
    try:
        dev_collector._state_set(conn, "feishu_directory_sync_status", "failure")
        dev_collector._state_set(conn, "feishu_directory_sync_last_success", "2026-06-18T10:00:00Z")
        dev_collector._state_set(conn, "feishu_directory_sync_last_attempt", "2026-06-18T11:00:00Z")
        dev_collector._state_set(conn, "feishu_directory_sync_last_error", "feishu unavailable")
        dev_collector._state_set(
            conn,
            "feishu_directory_sync_visibility_warnings",
            '[{"dept_id":"d1","expected":3,"got":1}]',
        )
        conn.commit()

        handler = _DummyHandler()
        dev_collector.H._meta(handler, conn)
    finally:
        conn.close()

    assert handler.code == 200
    assert handler.payload["feishu_directory_sync"]["status"] == "failure"
    assert handler.payload["feishu_directory_sync"]["last_success"] == "2026-06-18T10:00:00Z"
    assert handler.payload["feishu_directory_sync"]["last_attempt"] == "2026-06-18T11:00:00Z"
    assert handler.payload["feishu_directory_sync"]["last_error"] == "feishu unavailable"
    assert handler.payload["feishu_directory_sync"]["visibility_warnings_count"] == 1


def test_meta_redacts_feishu_sync_diagnostics_for_non_admin(monkeypatch, tmp_path):
    monkeypatch.setattr(dev_collector, "DB", str(tmp_path / "tok.db"))
    conn = dev_collector.db()
    try:
        dev_collector._state_set(conn, "feishu_directory_sync_status", "failure")
        dev_collector._state_set(conn, "feishu_directory_sync_last_success", "2026-06-18T10:00:00Z")
        dev_collector._state_set(conn, "feishu_directory_sync_last_attempt", "2026-06-18T11:00:00Z")
        dev_collector._state_set(conn, "feishu_directory_sync_last_error", "tenant token denied")
        dev_collector._state_set(
            conn,
            "feishu_directory_sync_visibility_warnings",
            '[{"dept_id":"secret","path":"Keep/Secret","expected":9,"got":0}]',
        )
        conn.commit()

        handler = _DummyHandler()
        handler._scope_user = {"email": "emp@keep.com", "is_admin": False, "scope": "self"}
        dev_collector.H._meta(handler, conn)
    finally:
        conn.close()

    sync = handler.payload["feishu_directory_sync"]
    assert sync["status"] == "failure"
    assert sync["last_success"] == "2026-06-18T10:00:00Z"
    assert "last_error" not in sync
    assert "visibility_warnings" not in sync
    assert sync["visibility_warnings_count"] == 1


def test_raw_admin_api_includes_attribution_audit_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(dev_collector, "DB", str(tmp_path / "tok.db"))
    conn = dev_collector.db()
    try:
        _insert_usage(conn, "supplier@keep.com", "Keep/合作商/W/供应商(SP000001)",
                      "lifetime", "all", "subscription", "Claude Code", 100, 1.0, 1)
        conn.execute(
            "UPDATE usage SET raw_dept=?, effective_dept=?, spend_bucket=?, attribution_source=?"
            " WHERE email=?",
            (
                "Keep/合作商/W/供应商(SP000001)",
                "Keep/技术平台部/固件组",
                "pending_business_outsourcing",
                "leader_department",
                "supplier@keep.com",
            ),
        )
        conn.commit()

        handler = _DummyHandler()
        dev_collector.H._raw(handler, conn)
    finally:
        conn.close()

    row = handler.payload["rows"][0]
    assert row["raw_dept"] == "Keep/合作商/W/供应商(SP000001)"
    assert row["effective_dept"] == "Keep/技术平台部/固件组"
    assert row["spend_bucket"] == "pending_business_outsourcing"
    assert row["attribution_source"] == "leader_department"


def test_governance_metrics_respect_configured_excluded_accounts(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADERBOARD_EXCLUDE_EMAILS", "outlier@example.com")
    dc = importlib.reload(dev_collector)
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    conn = dc.db()
    try:
        _insert_usage(conn, "normal@example.com", "Keep/A", "lifetime", "all",
                      "subscription", "Claude Code", 100, 1.0, 1)
        _insert_usage(conn, "outlier@example.com", "Keep/A", "lifetime", "all",
                      "subscription", "Claude Code", 10_000, 100.0, 10)
        _insert_usage(conn, "normal@example.com", "Keep/A", "day", "2026-06-18",
                      "subscription", "Claude Code", 10, 0.1, 1)
        _insert_usage(conn, "outlier@example.com", "Keep/A", "day", "2026-06-18",
                      "subscription", "Claude Code", 1_000, 10.0, 10)
        conn.commit()

        default = _DummyHandler()
        dc.H._governance_metrics(default, conn, {})
        included = _DummyHandler()
        dc.H._governance_metrics(included, conn, {"include_excluded": ["1"]})
    finally:
        conn.close()

    assert default.payload["summary"]["lifetime"]["tokens"] == 100
    assert default.payload["summary"]["day"]["tokens"] == 10
    assert default.payload["summary"]["sources"] == [
        {"source": "subscription", "users": 1, "tokens": 100, "cost": 1.0}
    ]

    assert included.payload["summary"]["lifetime"]["tokens"] == 10_100
    assert included.payload["summary"]["day"]["tokens"] == 1_010
