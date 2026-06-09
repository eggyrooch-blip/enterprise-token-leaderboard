import pathlib
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
