import pathlib
import sqlite3
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import dev_collector  # noqa: E402


def test_db_creates_parent_directory_for_dev_db(monkeypatch, tmp_path):
    db_path = tmp_path / "missing-parent" / "tok.db"
    monkeypatch.setattr(dev_collector, "DB", str(db_path))

    conn = dev_collector.db()
    conn.close()

    assert db_path.exists()


def test_db_migrates_report_log_os_column(monkeypatch, tmp_path):
    db_path = tmp_path / "tok.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE report_log("
        "serial TEXT PRIMARY KEY, email TEXT, hostname TEXT, ip TEXT, "
        "via TEXT NOT NULL DEFAULT 'mdm', reported_at TEXT)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(dev_collector, "DB", str(db_path))

    conn = dev_collector.db()
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(report_log)").fetchall()}
    finally:
        conn.close()

    assert "os" in columns


class _TokscaleReportHandler:
    def __init__(self, payload):
        self.payload = payload

    def _auth(self):
        return True

    def _read_body(self):
        return self.payload

    def _send(self, code, obj):
        self.code = code
        self.response = obj


def test_tokscale_report_persists_os_for_device_audit(monkeypatch, tmp_path):
    db_path = tmp_path / "tok.db"
    monkeypatch.setattr(dev_collector, "DB", str(db_path))
    monkeypatch.setattr(
        dev_collector,
        "_resolve_serial",
        lambda serial: {"email": "win-user@example.com", "department": "Eng"},
    )
    handler = _TokscaleReportHandler({
        "serial": "WIN-SERIAL-001",
        "hostname": "WIN-DEVICE-01",
        "os": "Microsoft Windows 11 10.0.22631",
        "ip": "192.0.2.10",
        "via": "mdm",
        "models": {"entries": []},
        "monthly": {"entries": []},
        "graph": {"contributions": []},
    })

    dev_collector.H._tokscale_report(handler)
    conn = dev_collector.db()
    try:
        os_label = conn.execute(
            "SELECT os FROM report_log WHERE serial=?",
            ("WIN-SERIAL-001",),
        ).fetchone()[0]
    finally:
        conn.close()

    assert handler.code == 200
    assert os_label == "Microsoft Windows 11 10.0.22631"
