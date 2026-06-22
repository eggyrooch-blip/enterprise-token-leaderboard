# -*- coding: utf-8 -*-
"""Tests for collector/feishu_directory_sync.py (Tasks 1-3).

Pure unit tests with injected fake Feishu API responses — no live network.
"""
import os
import json
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))
import feishu_directory_sync as fds  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures: a small fake org with a real employee dept and 3 supplier depts.
# --------------------------------------------------------------------------- #
def _departments():
    return [
        {"dept_id": "d_tech", "parent_id": "0", "name": "技术平台部"},
        {"dept_id": "d_fw", "parent_id": "d_tech", "name": "固件组",
         "leader_user_id": "ou_leader"},
        {"dept_id": "d_mkt_root", "parent_id": "0", "name": "运动消费事业部"},
        {"dept_id": "d_mkt", "parent_id": "d_mkt_root", "name": "市场营销部",
         "leader_user_id": "ou_owner"},
        # supplier root tree 合作商/W/*
        {"dept_id": "d_partner", "parent_id": "0", "name": "合作商"},
        {"dept_id": "d_w", "parent_id": "d_partner", "name": "W"},
        {"dept_id": "d_sp_leader", "parent_id": "d_w",
         "name": "中软国际科技服务有限公司(SP004867)", "leader_user_id": "ou_leader"},
        {"dept_id": "d_sp_chat", "parent_id": "d_w",
         "name": "北京再作品牌管理有限公司(SP000083)", "chat_id": "oc_supplier"},
        {"dept_id": "d_sp_dark", "parent_id": "d_w",
         "name": "成都涉泊科技有限公司(SP006910)"},
    ]


def _users():
    return [
        {"open_id": "ou_leader", "user_id": "u1", "email": "leader@keep.com",
         "name": "组长", "dept_id": "d_fw"},
        {"open_id": "ou_owner", "user_id": "u2", "email": "owner@keep.com",
         "name": "市场负责人", "dept_id": "d_mkt"},
        {"open_id": "ou_emp", "user_id": "u3", "email": "emp@keep.com",
         "name": "员工", "dept_id": "d_tech"},
        # emailless supplier users live only in feishu_users
        {"open_id": "ou_sup1", "user_id": "", "email": "", "name": "供应商甲",
         "dept_id": "d_sp_leader"},
        {"open_id": "ou_sup2", "user_id": "", "email": "", "name": "供应商乙",
         "dept_id": "d_sp_chat"},
    ]


def _paths():
    deps = _departments()
    for d in deps:
        d.setdefault("path", None)
    pbi = fds.build_department_paths(deps)
    for d in deps:
        d["path"] = pbi[d["dept_id"]]
    return deps, pbi


def _attributions(chat_owner=None):
    deps, pbi = _paths()
    users = _users()
    for u in users:
        u["dept_path"] = pbi.get(u["dept_id"], "")
    lookup = (lambda cid: {"oc_supplier": "ou_owner"}.get(cid, "")) if chat_owner else None
    return fds.derive_department_attributions(deps, users, chat_owner_lookup=lookup)


class _FakeDirectoryClient:
    def fetch_snapshot(self, root="0"):
        deps, pbi = _paths()
        users = _users()
        for u in users:
            u["dept_path"] = pbi.get(u["dept_id"], "")
        return deps, users

    def validate_visibility_coverage(self, departments, users):
        return []


class _LegacySqliteConnection:
    """Test wrapper that simulates production SQLite without UPSERT syntax."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        if "ON CONFLICT" in sql.upper():
            raise sqlite3.OperationalError('near "ON": syntax error')
        return self._conn.execute(sql, params)

    def commit(self):
        return self._conn.commit()


# --------------------------------------------------------------------------- #
# canonical_dept_key
# --------------------------------------------------------------------------- #
def test_canonical_key_feilian_and_feishu_match():
    feilian = "Keep/合作商/W/北京再作品牌管理有限公司(SP000083)"
    feishu = "合作商/W/北京再作品牌管理有限公司(SP000083)"
    assert fds.canonical_dept_key(feilian) == fds.canonical_dept_key(feishu)
    # supplier code preserved
    assert "(SP000083)" in fds.canonical_dept_key(feilian)


def test_canonical_key_normalizes_slashes_and_whitespace():
    assert fds.canonical_dept_key("Keep// 技术平台部 //固件组") == "技术平台部/固件组"


def test_is_outsourcing_department():
    assert fds.is_outsourcing_department("合作商/W/中软国际(SP004867)")
    assert not fds.is_outsourcing_department("技术平台部/固件组")


# --------------------------------------------------------------------------- #
# Task 3: attribution rules
# --------------------------------------------------------------------------- #
def test_leader_owned_supplier_maps_high_active():
    attrs = {a["source_dept_id"]: a for a in _attributions()}
    a = attrs["d_sp_leader"]
    assert a["rule"] == fds.RULE_LEADER
    assert a["confidence"] == fds.CONF_HIGH
    assert a["active"] == 1
    assert a["spend_bucket"] == fds.BUCKET_BUSINESS
    assert a["target_dept_path"] == "技术平台部/固件组"


def test_chat_owner_supplier_is_medium_inactive_suggestion():
    attrs = {a["source_dept_id"]: a for a in _attributions(chat_owner=True)}
    a = attrs["d_sp_chat"]
    assert a["rule"] == fds.RULE_CHAT_OWNER
    assert a["confidence"] == fds.CONF_MEDIUM
    assert a["active"] == 0  # not promoted yet
    assert a["target_dept_path"] == "运动消费事业部/市场营销部"


def test_group_owner_user_id_supplier_is_pending_without_lookup():
    deps, pbi = _paths()
    for d in deps:
        if d["dept_id"] == "d_sp_chat":
            d["group_owner_user_id"] = "ou_owner"
    users = _users()
    for u in users:
        u["dept_path"] = pbi.get(u["dept_id"], "")

    attrs = {a["source_dept_id"]: a for a in fds.derive_department_attributions(deps, users)}
    a = attrs["d_sp_chat"]

    assert a["rule"] == fds.RULE_CHAT_OWNER
    assert a["spend_bucket"] == fds.BUCKET_PENDING_BUSINESS
    assert a["active"] == 0
    assert a["target_dept_path"] == "运动消费事业部/市场营销部"


def test_unreadable_owner_supplier_is_unresolved():
    attrs = {a["source_dept_id"]: a for a in _attributions()}
    a = attrs["d_sp_dark"]
    assert a["rule"] == fds.RULE_UNRESOLVED
    assert a["confidence"] == fds.CONF_REVIEW
    assert a["active"] == 0
    # does not rewrite to a guessed department
    assert a["target_dept_path"] == a["source_dept_path"]


def test_non_outsourcing_is_direct_employee_bucket():
    attrs = {a["source_dept_id"]: a for a in _attributions()}
    a = attrs["d_fw"]
    assert a["rule"] == fds.RULE_DIRECT
    assert a["spend_bucket"] == fds.BUCKET_EMPLOYEE
    assert a["active"] == 1


def test_personnel_outsourcing_v_source_maps_to_internal_department():
    departments = [
        {"dept_id": "d_tech", "parent_id": "0", "name": "技术平台部",
         "leader_user_id": "ou_yuguangcan"},
        {"dept_id": "d_info", "parent_id": "d_tech", "name": "信息化技术部",
         "leader_user_id": "ou_hanmeng"},
        {"dept_id": "d_info_rd", "parent_id": "d_info", "name": "信息化研发组",
         "leader_user_id": "ou_hudi"},
        {"dept_id": "d_partner", "parent_id": "0", "name": "合作商"},
        {"dept_id": "d_v", "parent_id": "d_partner", "name": "V"},
        {"dept_id": "00045_v", "parent_id": "d_v",
         "name": "技术平台部-信息化技术部-信息化研发组",
         "leader_user_id": "ou_hudi"},
    ]
    path_by_id = fds.build_department_paths(departments)
    for d in departments:
        d["path"] = path_by_id[d["dept_id"]]
    users = [
        {"open_id": "ou_yuguangcan", "user_id": "yuguangcan",
         "email": "yuguangcan@keep.com", "dept_id": "d_tech",
         "dept_path": path_by_id["d_tech"]},
        {"open_id": "ou_hanmeng", "user_id": "hanmeng",
         "email": "hanmeng@keep.com", "dept_id": "d_info",
         "dept_path": path_by_id["d_info"]},
        {"open_id": "ou_hudi", "user_id": "hudi",
         "email": "hudi@keep.com", "dept_id": "d_info_rd",
         "dept_path": path_by_id["d_info_rd"]},
        {"open_id": "ou_chenghaichao", "user_id": "chenghaichao_v",
         "email": "chenghaichao_v@keep.com", "dept_id": "00045_v",
         "dept_path": path_by_id["00045_v"]},
    ]

    attrs = {a["source_dept_id"]: a for a in
             fds.derive_department_attributions(departments, users)}

    a = attrs["00045_v"]
    assert a["source_dept_path"] == "合作商/V/技术平台部-信息化技术部-信息化研发组"
    assert a["target_dept_id"] == "d_info_rd"
    assert a["target_dept_path"] == "技术平台部/信息化技术部/信息化研发组"
    assert a["spend_bucket"] == fds.BUCKET_EMPLOYEE
    assert a["active"] == 1


def test_key_conflict_marks_both_inactive():
    deps, pbi = _paths()
    # two distinct ids that normalize to the same key
    deps.append({"dept_id": "d_dupe", "parent_id": "d_w",
                 "name": "中软国际科技服务有限公司(SP004867)", "leader_user_id": "ou_leader",
                 "path": "合作商/W/中软国际科技服务有限公司(SP004867)"})
    users = _users()
    for u in users:
        u["dept_path"] = pbi.get(u["dept_id"], "")
    rows = {a["source_dept_id"]: a for a in
            fds.derive_department_attributions(deps, users)}
    assert rows["d_sp_leader"]["reason"] == "key_conflict"
    assert rows["d_dupe"]["reason"] == "key_conflict"
    assert rows["d_sp_leader"]["active"] == 0


def test_emailless_supplier_rolls_up_via_canonical_key():
    attrs = _attributions()
    feilian_raw = "Keep/合作商/W/中软国际科技服务有限公司(SP004867)"
    eff, bucket, src = fds.effective_dept_for_person(feilian_raw, attrs)
    assert eff == "技术平台部/固件组"
    assert bucket == fds.BUCKET_BUSINESS
    assert src == fds.RULE_LEADER


def test_chat_owner_candidate_rolls_up_as_pending_bucket():
    attrs = _attributions(chat_owner=True)
    feilian_raw = "Keep/合作商/W/北京再作品牌管理有限公司(SP000083)"
    eff, bucket, src = fds.effective_dept_for_person(feilian_raw, attrs)
    assert eff == "运动消费事业部/市场营销部"
    assert bucket == fds.BUCKET_PENDING_BUSINESS
    assert src == fds.RULE_CHAT_OWNER


def test_resolved_rate_excludes_inactive_chat_owner():
    # only leader-owned supplier is active among 3 suppliers -> 1/3
    attrs = _attributions(chat_owner=True)
    rate = fds.resolved_business_outsourcing_rate(attrs)
    assert abs(rate - (1.0 / 3.0)) < 1e-9


# --------------------------------------------------------------------------- #
# Task 1: schema + snapshot writer + idempotency
# --------------------------------------------------------------------------- #
def test_ensure_tables_creates_directory_tables():
    conn = sqlite3.connect(":memory:")
    fds.ensure_tables(conn)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"people", "feishu_users", "departments",
            "department_attributions", "roles"} <= names
    cols = {r[1] for r in conn.execute("PRAGMA table_info(people)").fetchall()}
    assert {"feishu_open_id", "effective_dept", "spend_bucket"} <= cols


def test_snapshot_keeps_emailless_users_only_in_feishu_users():
    deps, _ = _paths()
    conn = sqlite3.connect(":memory:")
    fds.write_directory_snapshot(conn, _users(), deps,
                                 admin_emails=["sunke@keep.com"], synced_at=1)
    fu = {r[0] for r in conn.execute("SELECT open_id FROM feishu_users").fetchall()}
    assert {"ou_sup1", "ou_sup2"} <= fu  # supplier rows present
    ppl = {r[0] for r in conn.execute("SELECT email FROM people").fetchall()}
    assert "" not in ppl
    assert "leader@keep.com" in ppl
    # emailless suppliers did NOT leak into people
    assert all(e for e in ppl)


def test_snapshot_writes_owner_and_admin_roles():
    deps, _ = _paths()
    conn = sqlite3.connect(":memory:")
    fds.write_directory_snapshot(conn, _users(), deps,
                                 admin_emails=["sunke@keep.com"], synced_at=1)
    roles = conn.execute(
        "SELECT email, role FROM roles ORDER BY email, role").fetchall()
    assert ("leader@keep.com", "department_owner") in roles
    assert ("owner@keep.com", "department_owner") in roles
    assert ("sunke@keep.com", "admin") in roles


def test_snapshot_resolves_admin_user_ids_to_email_roles():
    deps, _ = _paths()
    conn = sqlite3.connect(":memory:")

    fds.write_directory_snapshot(
        conn, _users(), deps, admin_user_ids=["u2", "ou_leader"], synced_at=1)

    roles = conn.execute(
        "SELECT email, role FROM roles ORDER BY email, role").fetchall()
    assert ("owner@keep.com", "admin") in roles
    assert ("leader@keep.com", "admin") in roles


def test_snapshot_does_not_grant_owner_role_to_inactive_leader():
    deps, _ = _paths()
    users = _users()
    for u in users:
        if u["open_id"] == "ou_leader":
            u["status"] = "inactive"
    conn = sqlite3.connect(":memory:")

    result = fds.write_directory_snapshot(
        conn, users, deps, admin_emails=["sunke@keep.com"], synced_at=1)

    roles = conn.execute(
        "SELECT email, role FROM roles ORDER BY email, role").fetchall()
    assert ("leader@keep.com", "department_owner") not in roles
    assert ("owner@keep.com", "department_owner") in roles
    assert any(a["kind"] == "leader_inactive" for a in result["alerts"])


def test_snapshot_is_idempotent():
    deps, _ = _paths()
    conn = sqlite3.connect(":memory:")
    fds.write_directory_snapshot(conn, _users(), deps, synced_at=1, allow_partial=True)
    snap1 = conn.execute(
        "SELECT * FROM department_attributions ORDER BY source_dept_id").fetchall()
    fds.write_directory_snapshot(conn, _users(), deps, synced_at=1, allow_partial=True)
    snap2 = conn.execute(
        "SELECT * FROM department_attributions ORDER BY source_dept_id").fetchall()
    assert snap1 == snap2
    # row counts stable
    assert conn.execute("SELECT COUNT(*) FROM feishu_users").fetchone()[0] == 5


def test_snapshot_writer_does_not_require_sqlite_upsert_syntax():
    deps, _ = _paths()
    conn = sqlite3.connect(":memory:")
    legacy_conn = _LegacySqliteConnection(conn)

    fds.write_directory_snapshot(
        legacy_conn, _users(), deps, admin_emails=["sunke@keep.com"],
        synced_at=1, allow_partial=True)
    users2 = _users()
    for u in users2:
        if u["open_id"] == "ou_leader":
            u["name"] = "新组长"
    fds.write_directory_snapshot(
        legacy_conn, users2, deps, admin_emails=["sunke@keep.com"],
        synced_at=2, allow_partial=True)

    assert conn.execute("SELECT COUNT(*) FROM feishu_users").fetchone()[0] == 5
    assert conn.execute(
        "SELECT name FROM feishu_users WHERE open_id='ou_leader'"
    ).fetchone()[0] == "新组长"
    assert conn.execute(
        "SELECT COUNT(*) FROM roles WHERE source='feishu_sync'"
    ).fetchone()[0] == 4
    assert conn.execute(
        "SELECT COUNT(*) FROM department_attributions"
    ).fetchone()[0] == len(deps)


def test_role_override_deny_blocks_feishu_owner():
    deps, _ = _paths()
    conn = sqlite3.connect(":memory:")
    fds.ensure_tables(conn)
    # blanket deny (dept_id='') removes ALL department_owner roles for this email
    conn.execute("INSERT INTO role_overrides(email,role,dept_id,action,reason)"
                 " VALUES('leader@keep.com','department_owner','','deny','test')")
    fds.write_directory_snapshot(conn, _users(), deps, synced_at=1, allow_partial=True)
    roles = {(r[0], r[1]) for r in conn.execute("SELECT email, role FROM roles")}
    assert ("leader@keep.com", "department_owner") not in roles


def test_unjoinable_leader_raises_without_allow_partial():
    deps, pbi = _paths()
    users = [u for u in _users() if u["open_id"] != "ou_leader"]  # drop the leader
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ValueError):
        fds.write_directory_snapshot(conn, users, deps, synced_at=1)


def test_downgrade_protection_keeps_last_known_good():
    deps, _ = _paths()
    conn = sqlite3.connect(":memory:")
    fds.write_directory_snapshot(conn, _users(), deps, synced_at=1, allow_partial=True)
    # leader@keep.com leaves -> next sync can no longer resolve d_sp_leader
    users2 = [u for u in _users() if u["open_id"] != "ou_leader"]
    result = fds.write_directory_snapshot(conn, users2, deps, synced_at=2,
                                          allow_partial=True)
    row = conn.execute(
        "SELECT active, reason FROM department_attributions WHERE source_dept_id=?",
        ("d_sp_leader",)).fetchone()
    assert row[0] == 1  # still active (last-known-good preserved)
    assert "downgrade_blocked" in row[1]
    assert any(a["kind"] == "downgrade_blocked" for a in result["alerts"])


def test_cli_low_coverage_syncs_directory_but_blocks_business_rollup(monkeypatch, tmp_path):
    monkeypatch.setattr(fds, "FeishuDirectoryClient", lambda: _FakeDirectoryClient())
    db_path = tmp_path / "tok.db"

    rc = fds.main(["--db", str(db_path)])

    assert rc == 0
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM feishu_users").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM departments").fetchone()[0] == 9
        assert conn.execute(
            "SELECT COUNT(*) FROM roles WHERE role='department_owner'"
        ).fetchone()[0] == 3
        row = conn.execute(
            "SELECT target_dept_path, spend_bucket, rule, active, reason"
            " FROM department_attributions WHERE source_dept_id='d_sp_leader'"
        ).fetchone()
        state = dict(conn.execute("SELECT key, value FROM app_state").fetchall())
    finally:
        conn.close()

    assert row == (
        "技术平台部/固件组",
        fds.BUCKET_PENDING_BUSINESS,
        fds.RULE_LEADER,
        0,
        "production_enablement_blocked_low_coverage",
    )
    assert state["feishu_directory_sync_status"] == "success"
    assert state["feishu_directory_sync_production_enablement_blocked"] == "1"
    assert state["feishu_directory_sync_business_rollup_enabled"] == "0"
    assert state["feishu_directory_sync_resolved_business_outsourcing_rate"] == "0.3333"


def test_cli_allows_low_coverage_only_with_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setattr(fds, "FeishuDirectoryClient", lambda: _FakeDirectoryClient())
    db_path = tmp_path / "tok.db"

    rc = fds.main(["--db", str(db_path), "--allow-low-coverage"])

    assert rc == 0
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM department_attributions").fetchone()[0] > 0
        assert conn.execute(
            "SELECT spend_bucket, rule, active FROM department_attributions"
            " WHERE source_dept_id='d_sp_leader'"
        ).fetchone() == (fds.BUCKET_BUSINESS, fds.RULE_LEADER, 1)
    finally:
        conn.close()


def test_cli_snapshot_uses_group_owner_as_pending_candidate(monkeypatch, tmp_path):
    class Client(_FakeDirectoryClient):
        def fetch_snapshot(self, root="0"):
            deps, users = super().fetch_snapshot(root)
            for d in deps:
                if d["dept_id"] == "d_sp_chat":
                    d["group_owner_user_id"] = "ou_owner"
            return deps, users

    monkeypatch.setattr(fds, "FeishuDirectoryClient", lambda: Client())
    db_path = tmp_path / "tok.db"

    assert fds.main(["--db", str(db_path), "--allow-low-coverage"]) == 0
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT target_dept_path, spend_bucket, rule, active"
            " FROM department_attributions WHERE source_dept_id='d_sp_chat'"
        ).fetchone()
    finally:
        conn.close()

    assert row == (
        "运动消费事业部/市场营销部",
        fds.BUCKET_PENDING_BUSINESS,
        fds.RULE_CHAT_OWNER,
        0,
    )


def test_cli_resolves_leader_and_admin_ids_when_user_listing_is_empty(monkeypatch, tmp_path):
    class Client(_FakeDirectoryClient):
        def fetch_snapshot(self, root="0"):
            deps, _users = super().fetch_snapshot(root)
            return deps, []

        def get_user(self, user_id, user_id_type="open_id"):
            users = {
                ("ou_leader", "open_id"): {
                    "open_id": "ou_leader", "user_id": "u1",
                    "email": "leader@keep.com", "name": "组长", "dept_id": "d_fw",
                    "status": "active",
                },
                ("sunke", "user_id"): {
                    "open_id": "ou_sunke", "user_id": "sunke",
                    "email": "sunke@keep.com", "name": "孙可", "dept_id": "d_tech",
                    "status": "active",
                },
            }
            if (user_id, user_id_type) not in users:
                raise RuntimeError("not found")
            return dict(users[(user_id, user_id_type)])

    monkeypatch.setattr(fds, "FeishuDirectoryClient", lambda: Client())
    db_path = tmp_path / "tok.db"

    rc = fds.main([
        "--db", str(db_path),
        "--admin-user-ids", "sunke",
        "--allow-low-coverage",
        "--allow-partial",
    ])

    assert rc == 0
    conn = sqlite3.connect(str(db_path))
    try:
        roles = conn.execute(
            "SELECT email, role, dept_path FROM roles ORDER BY email, role, dept_path"
        ).fetchall()
        users = conn.execute(
            "SELECT open_id, user_id, email, dept_path FROM feishu_users"
            " WHERE open_id IN ('ou_leader','ou_sunke') ORDER BY open_id"
        ).fetchall()
    finally:
        conn.close()

    assert ("leader@keep.com", "department_owner", "技术平台部/固件组") in roles
    assert ("sunke@keep.com", "admin", "") in roles
    assert users == [
        ("ou_leader", "u1", "leader@keep.com", "技术平台部/固件组"),
        ("ou_sunke", "sunke", "sunke@keep.com", "技术平台部"),
    ]


def test_cli_records_user_detail_resolution_warnings(monkeypatch, tmp_path, capsys):
    class Client(_FakeDirectoryClient):
        def fetch_snapshot(self, root="0"):
            deps, _users = super().fetch_snapshot(root)
            deps.append({
                "dept_id": "d_missing", "parent_id": "d_tech", "name": "缺失负责人组",
                "leader_user_id": "ou_missing",
            })
            pbi = fds.build_department_paths(deps)
            for d in deps:
                d["path"] = pbi.get(d["dept_id"], d.get("name", ""))
            return deps, []

        def get_user(self, user_id, user_id_type="open_id"):
            if (user_id, user_id_type) == ("ou_leader", "open_id"):
                return {
                    "open_id": "ou_leader", "user_id": "u1",
                    "email": "leader@keep.com", "name": "组长", "dept_id": "d_fw",
                    "status": "active",
                }
            raise RuntimeError("not visible")

    monkeypatch.setattr(fds, "FeishuDirectoryClient", lambda: Client())
    db_path = tmp_path / "tok.db"

    rc = fds.main(["--db", str(db_path), "--allow-low-coverage", "--allow-partial"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert any(w["kind"] == "user_detail_unresolved" and w["user_id"] == "ou_missing"
               for w in out["user_resolution_warnings"])
    conn = sqlite3.connect(str(db_path))
    try:
        roles = conn.execute("SELECT email, role FROM roles").fetchall()
    finally:
        conn.close()
    assert ("leader@keep.com", "department_owner") in roles


def test_cli_loads_manual_overrides_file_and_outputs_rule_counts(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fds, "FeishuDirectoryClient", lambda: _FakeDirectoryClient())
    db_path = tmp_path / "tok.db"
    override_path = tmp_path / "department-overrides.json"
    override_path.write_text(json.dumps({
        "合作商/W/北京再作品牌管理有限公司(SP000083)": {
            "target_dept_id": "d_mkt",
            "target_dept_path": "运动消费事业部/市场营销部",
            "spend_bucket": fds.BUCKET_BUSINESS,
        }
    }, ensure_ascii=False), encoding="utf-8")

    rc = fds.main([
        "--db", str(db_path),
        "--manual-overrides", str(override_path),
        "--allow-low-coverage",
    ])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT target_dept_id, target_dept_path, spend_bucket, rule, active"
            " FROM department_attributions WHERE source_dept_id='d_sp_chat'"
        ).fetchone()
    finally:
        conn.close()

    assert row == (
        "d_mkt",
        "运动消费事业部/市场营销部",
        fds.BUCKET_BUSINESS,
        fds.RULE_MANUAL,
        1,
    )
    assert out["manual_overrides"] == 1
    assert out["attribution_counts_by_rule"][fds.RULE_MANUAL] == 1
    assert out["attribution_counts_by_rule"][fds.RULE_UNRESOLVED] == 1


def test_cli_records_failure_health_without_erasing_last_success(monkeypatch, tmp_path):
    monkeypatch.setattr(fds, "FeishuDirectoryClient", lambda: _FakeDirectoryClient())
    db_path = tmp_path / "tok.db"

    assert fds.main(["--db", str(db_path), "--allow-low-coverage"]) == 0

    class BrokenDirectoryClient:
        def fetch_snapshot(self, root="0"):
            raise RuntimeError("feishu unavailable")

    monkeypatch.setattr(fds, "FeishuDirectoryClient", lambda: BrokenDirectoryClient())

    rc = fds.main(["--db", str(db_path)])

    assert rc == 1
    conn = sqlite3.connect(str(db_path))
    try:
        state = dict(conn.execute("SELECT key, value FROM app_state").fetchall())
        users = conn.execute("SELECT COUNT(*) FROM feishu_users").fetchone()[0]
    finally:
        conn.close()

    assert users == 5
    assert state["feishu_directory_sync_status"] == "failure"
    assert state["feishu_directory_sync_last_success"]
    assert "feishu unavailable" in state["feishu_directory_sync_last_error"]


# --------------------------------------------------------------------------- #
# Task 2: pagination adapter with fake _json_request
# --------------------------------------------------------------------------- #
class _FakeApi(object):
    """Records calls and returns canned paginated responses."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, payload=None, headers=None, method=None):
        self.calls.append((url, payload, dict(headers or {})))
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "t-xyz"}
        if "/children" in url:
            # page through two pages for root, none for leaf parents
            if "departments/0/children" in url:
                if "page_token=p2" in url:
                    return {"code": 0, "data": {"has_more": False, "items": [
                        {"department_id": "d_partner", "parent_department_id": "0",
                         "name": "合作商"}]}}
                return {"code": 0, "data": {"has_more": True, "page_token": "p2",
                        "items": [
                            {"department_id": "d_tech", "parent_department_id": "0",
                             "name": "技术平台部", "member_count": 1}]}}
            return {"code": 0, "data": {"has_more": False, "items": []}}
        if "find_by_department" in url:
            if "page_token=u2" in url:
                return {"code": 0, "data": {"has_more": False, "items": [
                    {"open_id": "ou_emp", "user_id": "u3", "email": "emp@keep.com",
                     "name": "员工", "department_ids": ["d_tech"],
                     "status": {"is_resigned": False}}]}}
            return {"code": 0, "data": {"has_more": True, "page_token": "u2",
                    "items": [
                        {"open_id": "ou_sup1", "user_id": "", "email": "",
                         "name": "供应商甲", "department_ids": ["d_partner"],
                         "status": {"is_resigned": False}}]}}
        return {"code": 0, "data": {}}


def test_client_paginates_departments_with_page_size_50():
    api = _FakeApi()
    client = fds.FeishuDirectoryClient(app_id="a", app_secret="b", json_request=api)
    deps = client.list_departments(root="0")
    ids = {d["dept_id"] for d in deps}
    assert {"d_tech", "d_partner"} <= ids
    # page_size=50 used, never 100, and no fetch_child on children endpoint
    child_calls = [u for (u, _, _) in api.calls if "/children" in u]
    assert child_calls
    for u in child_calls:
        assert "page_size=50" in u
        assert "page_size=100" not in u
        assert "fetch_child" not in u
        assert "department_id_type=department_id" in u
        assert "user_id_type=open_id" in u


def test_client_paginates_users_and_keeps_open_and_user_id():
    api = _FakeApi()
    client = fds.FeishuDirectoryClient(app_id="a", app_secret="b", json_request=api)
    users = client.list_users_by_department("0", fetch_child=True)
    by_open = {u["open_id"]: u for u in users}
    assert {"ou_sup1", "ou_emp"} <= set(by_open)
    assert by_open["ou_emp"]["user_id"] == "u3"
    assert by_open["ou_sup1"]["email"] == ""  # emailless supplier preserved
    user_calls = [u for (u, _, _) in api.calls if "find_by_department" in u]
    assert all("user_id_type=open_id" in u for u in user_calls)


def test_visibility_coverage_flags_partial():
    client = fds.FeishuDirectoryClient(app_id="a", app_secret="b",
                                       json_request=_FakeApi())
    deps = [{"dept_id": "d_tech", "path": "技术平台部", "member_count": 3}]
    users = [{"open_id": "ou_emp", "dept_id": "d_tech"}]
    warns = client.validate_visibility_coverage(deps, users)
    assert warns and warns[0]["dept_id"] == "d_tech"
    assert warns[0]["expected"] == 3 and warns[0]["got"] == 1
