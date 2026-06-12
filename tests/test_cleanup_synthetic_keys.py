import pathlib
import sqlite3
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "collector"
SCRIPT = COLLECTOR / "cleanup_synthetic_keys.py"
sys.path.insert(0, str(COLLECTOR))

import dev_collector  # noqa: E402


def _insert_usage(conn, email, dept, period_type, period, source, client, provider, model,
                  input_, output, cache_read, cache_write, reasoning, total, cost, messages):
    conn.execute(dev_collector._UPSERT_SQL, (
        email, dept, period_type, period, source, client, provider, model,
        input_, output, cache_read, cache_write, reasoning, total, cost, messages,
    ))


def _run_cleanup(db_path, synthetic, target):
    return subprocess.run(
        [sys.executable, str(SCRIPT), synthetic, target, "--db", str(db_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )


def test_cleanup_synthetic_keys_merges_usage_deletes_people_and_is_idempotent(monkeypatch, tmp_path):
    db_path = tmp_path / "tok.db"
    monkeypatch.setattr(dev_collector, "DB", str(db_path))
    conn = dev_collector.db()
    synthetic = "litellm-key:zhangyiqi-202606030074"
    target = "zhangyiqi@keep.com"
    try:
        conn.execute(
            "INSERT INTO people(email, name, avatar, dept) VALUES(?,?,?,?)",
            (synthetic, "synthetic", "", "unknown"),
        )
        conn.execute(
            "INSERT INTO people(email, name, avatar, dept) VALUES(?,?,?,?)",
            (target, "张一淇", "", "Keep/平台/基础"),
        )
        _insert_usage(
            conn, target, "Keep/平台/基础", "day", "2026-06-10", "litellm",
            "LiteLLM", "", "gpt-4o", 10, 5, 1, 2, 3, 21, 1.5, 2,
        )
        _insert_usage(
            conn, synthetic, "unknown", "day", "2026-06-10", "litellm",
            "LiteLLM", "", "gpt-4o", 20, 7, 3, 4, 5, 39, 2.25, 4,
        )
        _insert_usage(
            conn, synthetic, "unknown", "month", "2026-06", "litellm",
            "LiteLLM", "", "gpt-4.1", 6, 5, 4, 3, 2, 20, 0.75, 1,
        )
        conn.commit()
    finally:
        conn.close()

    first = _run_cleanup(db_path, synthetic, target)
    assert "BEFORE synthetic_usage=2 target_usage=1 synthetic_people=1" in first.stdout
    assert "AFTER synthetic_usage=0 target_usage=2 synthetic_people=0" in first.stdout

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM usage WHERE email=?", (synthetic,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM people WHERE email=?", (synthetic,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM usage WHERE email=?", (target,)).fetchone()[0] == 2

        merged = conn.execute(
            "SELECT dept,input,output,cache_read,cache_write,reasoning,total,cost,messages"
            " FROM usage WHERE email=? AND period_type='day' AND period='2026-06-10'"
            " AND source='litellm' AND client='LiteLLM' AND provider='' AND model='gpt-4o'",
            (target,),
        ).fetchone()
        assert merged == ("Keep/平台/基础", 30, 12, 4, 6, 8, 60, 3.75, 6)

        rekeyed = conn.execute(
            "SELECT dept,input,output,cache_read,cache_write,reasoning,total,cost,messages"
            " FROM usage WHERE email=? AND period_type='month' AND period='2026-06'"
            " AND source='litellm' AND client='LiteLLM' AND provider='' AND model='gpt-4.1'",
            (target,),
        ).fetchone()
        assert rekeyed == ("unknown", 6, 5, 4, 3, 2, 20, 0.75, 1)
    finally:
        conn.close()

    second = _run_cleanup(db_path, synthetic, target)
    assert "BEFORE synthetic_usage=0 target_usage=2 synthetic_people=0" in second.stdout
    assert "AFTER synthetic_usage=0 target_usage=2 synthetic_people=0" in second.stdout
