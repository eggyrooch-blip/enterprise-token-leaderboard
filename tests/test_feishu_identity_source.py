import importlib
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import dev_collector  # noqa: E402


class _Handler:
    def __init__(self, payload):
        self.payload = payload

    def _auth(self):
        return True

    def _read_body(self):
        return self.payload

    def _send(self, code, obj):
        self.code = code
        self.response = obj


def _payload(serial="SER-001"):
    return {
        "serial": serial,
        "via": "mdm",
        "models": {"entries": [
            {"client": "claude", "provider": "anthropic", "model": "sonnet", "input": 10, "output": 5},
        ]},
        "monthly": {"entries": [
            {"month": "2026-06", "input": 10, "output": 5},
        ]},
        "graph": {"contributions": [
            {"date": "2026-06-18", "clients": [
                {"client": "claude", "providerId": "anthropic", "modelId": "sonnet",
                 "tokens": {"input": 3, "output": 2}, "cost": 0.01, "messages": 1},
            ]},
        ]},
    }


def test_tokscale_report_maps_sn_supplier_dept_through_active_feishu_attribution(monkeypatch, tmp_path):
    dc = importlib.reload(dev_collector)
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    monkeypatch.setattr(
        dc,
        "_resolve_serial",
        lambda serial: {
            "name": "供应商设备",
            "department": "Keep/合作商/W/中软国际科技服务有限公司(SP004867)",
        },
    )
    conn = dc.db()
    conn.execute(
        "CREATE TABLE department_attributions("
        "source_dept_id TEXT PRIMARY KEY, source_dept_key TEXT NOT NULL,"
        "source_dept_path TEXT NOT NULL, target_dept_id TEXT DEFAULT '',"
        "target_dept_path TEXT DEFAULT '', spend_bucket TEXT NOT NULL,"
        "rule TEXT NOT NULL, confidence TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0,"
        "reason TEXT DEFAULT '', updated_at TEXT NOT NULL)"
    )
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

    handler = _Handler(_payload())
    dc.H._tokscale_report(handler)

    conn = dc.db()
    try:
        row = conn.execute(
            "SELECT email, dept, raw_dept, effective_dept, spend_bucket, attribution_source"
            " FROM usage WHERE period_type='lifetime'"
        ).fetchone()
        person = conn.execute(
            "SELECT dept, raw_dept, effective_dept, spend_bucket FROM people WHERE email=?",
            ("sn:SER-001",),
        ).fetchone()
        period_rows = conn.execute(
            "SELECT period_type, period, dept, effective_dept, spend_bucket"
            " FROM usage ORDER BY period_type, period"
        ).fetchall()
    finally:
        conn.close()

    assert handler.code == 200
    assert row == (
        "sn:SER-001",
        "Keep/技术平台部/固件组",
        "Keep/合作商/W/中软国际科技服务有限公司(SP004867)",
        "Keep/技术平台部/固件组",
        "business_outsourcing",
        "leader_department",
    )
    assert person == (
        "Keep/技术平台部/固件组",
        "Keep/合作商/W/中软国际科技服务有限公司(SP004867)",
        "Keep/技术平台部/固件组",
        "business_outsourcing",
    )
    assert period_rows == [
        ("day", "2026-06-18", "Keep/技术平台部/固件组", "Keep/技术平台部/固件组", "business_outsourcing"),
        ("lifetime", "all", "Keep/技术平台部/固件组", "Keep/技术平台部/固件组", "business_outsourcing"),
        ("month", "2026-06", "Keep/技术平台部/固件组", "Keep/技术平台部/固件组", "business_outsourcing"),
    ]


def test_tokscale_report_prefers_feishu_people_department_over_feilian(monkeypatch, tmp_path):
    dc = importlib.reload(dev_collector)
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    monkeypatch.setattr(
        dc,
        "_resolve_serial",
        lambda serial: {
            "email": "person@keep.com",
            "name": "飞连旧名",
            "department": "Keep/旧部门",
            "avatar": "old-avatar",
        },
    )
    conn = dc.db()
    conn.execute(
        "INSERT OR REPLACE INTO people(email,name,avatar,dept,raw_dept,effective_dept,spend_bucket,source)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (
            "person@keep.com",
            "飞书姓名",
            "feishu-avatar",
            "Keep/新部门/组",
            "Keep/新部门/组",
            "Keep/新部门/组",
            "employee_staff_outsourcing",
            "feishu",
        ),
    )
    conn.commit()
    conn.close()

    handler = _Handler(_payload("SER-002"))
    dc.H._tokscale_report(handler)

    conn = dc.db()
    try:
        usage = conn.execute(
            "SELECT email, dept, raw_dept, effective_dept, spend_bucket FROM usage"
            " WHERE period_type='lifetime'"
        ).fetchone()
        person = conn.execute(
            "SELECT name, avatar, dept, source FROM people WHERE email='person@keep.com'"
        ).fetchone()
    finally:
        conn.close()

    assert handler.code == 200
    assert usage == (
        "person@keep.com",
        "Keep/新部门/组",
        "Keep/新部门/组",
        "Keep/新部门/组",
        "employee_staff_outsourcing",
    )
    assert person == ("飞书姓名", "feishu-avatar", "Keep/新部门/组", "feishu")


def test_tokscale_report_does_not_let_non_feishu_people_override_feilian(monkeypatch, tmp_path):
    dc = importlib.reload(dev_collector)
    monkeypatch.setattr(dc, "DB", str(tmp_path / "tok.db"))
    monkeypatch.setattr(
        dc,
        "_resolve_serial",
        lambda serial: {
            "email": "person@keep.com",
            "name": "飞连姓名",
            "department": "Keep/飞连新部门",
            "avatar": "feilian-avatar",
        },
    )
    conn = dc.db()
    conn.execute(
        "INSERT OR REPLACE INTO people(email,name,avatar,dept,raw_dept,effective_dept,spend_bucket,source)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (
            "person@keep.com",
            "旧缓存姓名",
            "old-avatar",
            "Keep/旧缓存部门",
            "Keep/旧缓存部门",
            "Keep/旧缓存部门",
            "employee_staff_outsourcing",
            "feilian",
        ),
    )
    conn.commit()
    conn.close()

    handler = _Handler(_payload("SER-003"))
    dc.H._tokscale_report(handler)

    conn = dc.db()
    try:
        usage = conn.execute(
            "SELECT dept, raw_dept, effective_dept FROM usage WHERE period_type='lifetime'"
        ).fetchone()
        person = conn.execute(
            "SELECT name, avatar, dept, source FROM people WHERE email='person@keep.com'"
        ).fetchone()
    finally:
        conn.close()

    assert handler.code == 200
    assert usage == ("Keep/飞连新部门", "Keep/飞连新部门", "Keep/飞连新部门")
    assert person == ("飞连姓名", "feilian-avatar", "Keep/飞连新部门", "feilian")
