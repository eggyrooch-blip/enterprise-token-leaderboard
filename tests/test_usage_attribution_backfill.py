import importlib
import pathlib
import sqlite3
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import dev_collector  # noqa: E402


def _schema(conn):
    conn.executescript(
        """
        CREATE TABLE usage(
            email TEXT,
            dept TEXT,
            period_type TEXT,
            period TEXT,
            source TEXT,
            client TEXT,
            total INTEGER,
            cost REAL,
            messages INTEGER
        );
        CREATE TABLE department_attributions(
            source_dept_id TEXT PRIMARY KEY,
            source_dept_key TEXT NOT NULL,
            source_dept_path TEXT NOT NULL,
            target_dept_id TEXT DEFAULT '',
            target_dept_path TEXT DEFAULT '',
            spend_bucket TEXT NOT NULL,
            rule TEXT NOT NULL,
            confidence TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0,
            reason TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        );
        """
    )


def _usage(conn, email, dept, tokens=1):
    conn.execute(
        "INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?)",
        (email, dept, "lifetime", "all", "subscription", "Claude Code", tokens, 0.1, 1),
    )


def test_backfill_usage_attribution_preserves_raw_and_sets_effective_bucket():
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _usage(conn, "employee@keep.com", "技术平台部/固件组", 100)
    _usage(conn, "supplier@keep.com", "Keep/合作商/W/中软国际科技服务有限公司(SP004867)", 40)
    _usage(conn, "unknown@keep.com", "Keep/合作商/W/未解析供应商(SP009999)", 7)
    conn.execute(
        "INSERT INTO department_attributions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "dept-supplier",
            dc._canonical_dept_key("合作商/W/中软国际科技服务有限公司(SP004867)"),
            "合作商/W/中软国际科技服务有限公司(SP004867)",
            "dept-target",
            "Keep/技术平台部/固件组",
            "business_outsourcing",
            "leader_department",
            "high",
            1,
            "",
            "2026-06-18",
        ),
    )
    conn.commit()

    dry = dc._backfill_usage_attribution(conn, dry_run=True)
    written = dc._backfill_usage_attribution(conn, dry_run=False)

    rows = {
        row[0]: row[1:]
        for row in conn.execute(
            "SELECT email, raw_dept, effective_dept, dept, spend_bucket FROM usage"
        ).fetchall()
    }

    assert dry["would_update"] == 3
    assert written["updated"] == 3
    assert rows["employee@keep.com"] == (
        "技术平台部/固件组",
        "技术平台部/固件组",
        "技术平台部/固件组",
        "employee_staff_outsourcing",
    )
    assert rows["supplier@keep.com"] == (
        "Keep/合作商/W/中软国际科技服务有限公司(SP004867)",
        "Keep/技术平台部/固件组",
        "Keep/技术平台部/固件组",
        "business_outsourcing",
    )
    assert rows["unknown@keep.com"] == (
        "Keep/合作商/W/未解析供应商(SP009999)",
        "Keep/合作商/W/未解析供应商(SP009999)",
        "Keep/合作商/W/未解析供应商(SP009999)",
        "unresolved",
    )


def test_backfill_surfaces_inactive_chat_owner_candidate_as_pending():
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    raw = "Keep/合作商/W/北京再作品牌管理有限公司(SP000083)"
    _usage(conn, "candidate@keep.com", raw, 30)
    conn.execute(
        "INSERT INTO department_attributions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "dept-chat-candidate",
            dc._canonical_dept_key(raw),
            "合作商/W/北京再作品牌管理有限公司(SP000083)",
            "dept-target",
            "Keep/运动消费事业部/市场营销部",
            "pending_business_outsourcing",
            "chat_owner_department",
            "medium",
            0,
            "",
            "2026-06-18",
        ),
    )
    conn.commit()

    written = dc._backfill_usage_attribution(conn, dry_run=False)
    row = conn.execute(
        "SELECT raw_dept, effective_dept, dept, spend_bucket, attribution_source"
        " FROM usage WHERE email='candidate@keep.com'"
    ).fetchone()

    assert written["pending_business_outsourcing"] == 1
    assert row == (
        raw,
        "Keep/运动消费事业部/市场营销部",
        "Keep/运动消费事业部/市场营销部",
        "pending_business_outsourcing",
        "chat_owner_department",
    )


def test_backfill_surfaces_inactive_pending_business_candidate():
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    raw = "Keep/合作商/W/中软国际科技服务有限公司(SP004867)"
    _usage(conn, "candidate@keep.com", raw, 30)
    conn.execute(
        "INSERT INTO department_attributions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "dept-low-coverage-candidate",
            dc._canonical_dept_key(raw),
            "合作商/W/中软国际科技服务有限公司(SP004867)",
            "dept-target",
            "Keep/技术平台部/固件组",
            "pending_business_outsourcing",
            "leader_department",
            "high",
            0,
            "production_enablement_blocked_low_coverage",
            "2026-06-18",
        ),
    )
    conn.commit()

    written = dc._backfill_usage_attribution(conn, dry_run=False)
    row = conn.execute(
        "SELECT raw_dept, effective_dept, dept, spend_bucket, attribution_source"
        " FROM usage WHERE email='candidate@keep.com'"
    ).fetchone()

    assert written["pending_business_outsourcing"] == 1
    assert row == (
        raw,
        "Keep/技术平台部/固件组",
        "Keep/技术平台部/固件组",
        "pending_business_outsourcing",
        "leader_department",
    )


def test_db_startup_backfills_existing_legacy_usage_rows(monkeypatch, tmp_path):
    dc = importlib.reload(dev_collector)
    db_path = tmp_path / "tok.db"
    conn = sqlite3.connect(str(db_path))
    _schema(conn)
    _usage(conn, "supplier@keep.com", "Keep/合作商/W/中软国际科技服务有限公司(SP004867)", 40)
    conn.execute(
        "INSERT INTO department_attributions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "dept-supplier",
            dc._canonical_dept_key("合作商/W/中软国际科技服务有限公司(SP004867)"),
            "合作商/W/中软国际科技服务有限公司(SP004867)",
            "dept-target",
            "Keep/技术平台部/固件组",
            "business_outsourcing",
            "leader_department",
            "high",
            1,
            "",
            "2026-06-18",
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(dc, "DB", str(db_path))

    conn = dc.db()
    try:
        row = conn.execute(
            "SELECT raw_dept, effective_dept, dept, spend_bucket FROM usage"
        ).fetchone()
    finally:
        conn.close()

    assert row == (
        "Keep/合作商/W/中软国际科技服务有限公司(SP004867)",
        "Keep/技术平台部/固件组",
        "Keep/技术平台部/固件组",
        "business_outsourcing",
    )


def test_db_backfill_reruns_when_department_attributions_change(monkeypatch, tmp_path):
    dc = importlib.reload(dev_collector)
    db_path = tmp_path / "tok.db"
    conn = sqlite3.connect(str(db_path))
    _schema(conn)
    _usage(conn, "supplier@keep.com", "Keep/合作商/W/中软国际科技服务有限公司(SP004867)", 40)
    conn.commit()
    conn.close()
    monkeypatch.setattr(dc, "DB", str(db_path))

    first = dc.db()
    try:
        before = first.execute(
            "SELECT raw_dept, effective_dept, dept, spend_bucket FROM usage"
        ).fetchone()
        first.execute(
            "INSERT INTO department_attributions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "dept-supplier",
                dc._canonical_dept_key("合作商/W/中软国际科技服务有限公司(SP004867)"),
                "合作商/W/中软国际科技服务有限公司(SP004867)",
                "dept-target",
                "Keep/技术平台部/固件组",
                "business_outsourcing",
                "leader_department",
                "high",
                1,
                "",
                "2026-06-18T10:00:00",
            ),
        )
        first.commit()
    finally:
        first.close()

    second = dc.db()
    try:
        after = second.execute(
            "SELECT raw_dept, effective_dept, dept, spend_bucket FROM usage"
        ).fetchone()
    finally:
        second.close()

    assert before == (
        "Keep/合作商/W/中软国际科技服务有限公司(SP004867)",
        "Keep/合作商/W/中软国际科技服务有限公司(SP004867)",
        "Keep/合作商/W/中软国际科技服务有限公司(SP004867)",
        "unresolved",
    )
    assert after == (
        "Keep/合作商/W/中软国际科技服务有限公司(SP004867)",
        "Keep/技术平台部/固件组",
        "Keep/技术平台部/固件组",
        "business_outsourcing",
    )


def test_usage_backfill_needed_is_false_after_clean_startup(monkeypatch, tmp_path):
    dc = importlib.reload(dev_collector)
    db_path = tmp_path / "tok.db"
    conn = sqlite3.connect(str(db_path))
    _schema(conn)
    _usage(conn, "employee@keep.com", "Keep/技术平台部/固件组", 100)
    conn.commit()
    conn.close()
    monkeypatch.setattr(dc, "DB", str(db_path))

    conn = dc.db()
    try:
        assert dc._usage_backfill_needed(conn) is False
        assert conn.execute(
            "SELECT value FROM app_state WHERE key='usage_backfill_complete'"
        ).fetchone()[0] == "1"
    finally:
        conn.close()
