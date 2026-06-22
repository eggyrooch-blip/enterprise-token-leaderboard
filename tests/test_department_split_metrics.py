import datetime
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
            messages INTEGER,
            raw_dept TEXT,
            effective_dept TEXT,
            spend_bucket TEXT
        );
        CREATE TABLE people(email TEXT PRIMARY KEY, name TEXT, avatar TEXT, dept TEXT);
        CREATE TABLE feishu_member(email TEXT, name TEXT, dept TEXT, feature_key TEXT,
            credits REAL, usage_date TEXT, avatar TEXT, entity_id TEXT);
        CREATE TABLE departed(email TEXT PRIMARY KEY);
        """
    )


def _usage(conn, email, raw_dept, effective_dept, bucket, tokens, cost, messages):
    conn.execute(
        "INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            email,
            effective_dept,
            "lifetime",
            "all",
            "subscription",
            "Claude Code",
            tokens,
            cost,
            messages,
            raw_dept,
            effective_dept,
            bucket,
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO people VALUES(?,?,?,?)",
        (email, email.split("@")[0], "", raw_dept),
    )


def _teams(dc, conn, monkeypatch):
    monkeypatch.setattr(dc, "_dept_headcount_map", lambda *_a, **_k: {"Keep/技术平台部/固件组": 3})
    captured = {}

    class Fake:
        def _send(self, code, obj):
            captured["code"] = code
            captured["obj"] = obj

    dc.H._teams(Fake(), conn, {})
    assert captured["code"] == 200
    return {t["dept"]: t for t in captured["obj"]["teams"]}


def test_teams_return_full_employee_and_business_outsourcing_views(monkeypatch):
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _usage(
        conn,
        "employee@keep.com",
        "Keep/技术平台部/固件组",
        "Keep/技术平台部/固件组",
        "employee_staff_outsourcing",
        100,
        1.0,
        10,
    )
    _usage(
        conn,
        "supplier@keep.com",
        "Keep/合作商/W/中软国际科技服务有限公司(SP004867)",
        "Keep/技术平台部/固件组",
        "business_outsourcing",
        40,
        0.4,
        4,
    )
    _usage(
        conn,
        "pending@keep.com",
        "Keep/合作商/W/待确认供应商(SP009999)",
        "Keep/技术平台部/固件组",
        "pending_business_outsourcing",
        7,
        0.07,
        1,
    )
    conn.commit()

    team = _teams(dc, conn, monkeypatch)["Keep/技术平台部/固件组"]

    assert team["employee_staff_outsourcing"]["tokens"] == 100
    assert team["business_outsourcing"]["tokens"] == 40
    assert team["department_full"]["tokens"] == 140
    assert team["department_full"]["tokens"] == (
        team["employee_staff_outsourcing"]["tokens"]
        + team["business_outsourcing"]["tokens"]
    )
    assert team["department_full"]["cost"] == 1.4
    assert team["department_full"]["messages"] == 14
    assert team["pending_business_outsourcing"]["tokens"] == 7
    assert team["tokens"] == 140


def test_split_metrics_do_not_assert_active_user_additivity(monkeypatch):
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    today = datetime.date.today().isoformat()
    _usage(conn, "same@keep.com", "Keep/A/组", "Keep/A/组", "employee_staff_outsourcing", 10, 0.1, 1)
    conn.execute(
        "INSERT INTO feishu_member VALUES(?,?,?,?,?,?,?,?)",
        ("same@keep.com", "same", "Keep/A/组", "aily_credits", 100, today, "", ""),
    )
    conn.commit()

    team = _teams(dc, conn, monkeypatch)["Keep/A/组"]

    assert team["department_full"]["people"] == 1
    assert team["employee_staff_outsourcing"]["people"] == 1
    assert team["business_outsourcing"]["people"] == 0
    assert team["people"] == 1


def test_unresolved_business_outsourcing_is_surfaced_but_excluded_from_full(monkeypatch):
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    raw = "Keep/合作商/W/未解析供应商(SP009999)"
    _usage(conn, "unknown@keep.com", raw, raw, "unresolved", 9, 0.09, 2)
    conn.commit()

    teams = _teams(dc, conn, monkeypatch)
    node = teams["Keep/外部合作商/未解析供应商(SP009999)"]

    assert node["department_full"]["tokens"] == 0
    assert node["tokens"] == 0
    assert node["unresolved"]["tokens"] == 9
    assert node["unresolved"]["cost"] == 0.09
    assert node["unresolved"]["messages"] == 2


def test_aily_credits_follow_people_spend_bucket_when_available(monkeypatch):
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("ALTER TABLE people ADD COLUMN spend_bucket TEXT DEFAULT ''")
    today = datetime.date.today().isoformat()
    conn.execute(
        "INSERT INTO feishu_member VALUES(?,?,?,?,?,?,?,?)",
        ("supplier@keep.com", "supplier", "Keep/技术平台部/固件组", "aily_credits", 300, today, "", ""),
    )
    conn.execute(
        "INSERT INTO people(email,name,avatar,dept,spend_bucket) VALUES(?,?,?,?,?)",
        ("supplier@keep.com", "supplier", "", "Keep/技术平台部/固件组", "business_outsourcing"),
    )
    conn.commit()

    team = _teams(dc, conn, monkeypatch)["Keep/技术平台部/固件组"]

    assert team["employee_staff_outsourcing"]["credits"] == 0
    assert team["business_outsourcing"]["credits"] == 300
    assert team["department_full"]["credits"] == 300


def test_aily_credits_follow_people_effective_dept_for_supplier_raw_dept(monkeypatch):
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("ALTER TABLE people ADD COLUMN effective_dept TEXT DEFAULT ''")
    conn.execute("ALTER TABLE people ADD COLUMN spend_bucket TEXT DEFAULT ''")
    today = datetime.date.today().isoformat()
    raw = "Keep/合作商/W/中软国际科技服务有限公司(SP004867)"
    conn.execute(
        "INSERT INTO feishu_member VALUES(?,?,?,?,?,?,?,?)",
        ("supplier@keep.com", "supplier", raw, "aily_credits", 300, today, "", ""),
    )
    conn.execute(
        "INSERT INTO people(email,name,avatar,dept,effective_dept,spend_bucket)"
        " VALUES(?,?,?,?,?,?)",
        (
            "supplier@keep.com",
            "supplier",
            "",
            raw,
            "Keep/技术平台部/固件组",
            "business_outsourcing",
        ),
    )
    conn.commit()

    teams = _teams(dc, conn, monkeypatch)
    team = teams["Keep/技术平台部/固件组"]

    assert team["business_outsourcing"]["credits"] == 300
    assert team["department_full"]["credits"] == 300
    assert "Keep/外部合作商/中软国际科技服务有限公司(SP004867)" not in teams
