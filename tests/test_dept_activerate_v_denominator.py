#!/usr/bin/env python3
"""活跃率分母回归：人员外包 V 要进入真实部门 staff/total，业务外包 W 不进分母。

运行:
  python3 -m pytest tests/test_dept_activerate_v_denominator.py -q
  或 python3 tests/test_dept_activerate_v_denominator.py
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.join(os.path.dirname(HERE), "collector")
sys.path.insert(0, COLLECTOR)

os.environ.setdefault("DEV_DB", os.path.join(HERE, "_v_denominator_unused.db"))
import dev_collector as dc  # noqa: E402


def _reset_cache():
    dc._dept_headcount_mem = None
    dc._dept_headcount_source_used_mem = None
    try:
        if os.path.exists(dc._DEPT_HEADCOUNT_FILE):
            os.remove(dc._DEPT_HEADCOUNT_FILE)
    except Exception:
        pass


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
        CREATE TABLE people(
            email TEXT PRIMARY KEY,
            name TEXT,
            avatar TEXT,
            dept TEXT
        );
        CREATE TABLE feishu_member(
            email TEXT,
            name TEXT,
            dept TEXT,
            feature_key TEXT,
            credits REAL,
            usage_date TEXT,
            avatar TEXT,
            entity_id TEXT
        );
        CREATE TABLE departed(email TEXT PRIMARY KEY);
        CREATE TABLE departments(
            dept_id TEXT PRIMARY KEY,
            open_dept_id TEXT DEFAULT '',
            parent_id TEXT,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            leader_user_id TEXT DEFAULT '',
            chat_id TEXT DEFAULT '',
            group_owner_user_id TEXT DEFAULT '',
            member_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            updated_at TEXT DEFAULT ''
        );
        """
    )


def _usage(conn, email, dept, total):
    conn.execute(
        "INSERT INTO usage VALUES(?,?,'lifetime','all','subscription','Claude Code',?,1,5)",
        (email, dept, total),
    )
    conn.execute(
        "INSERT OR REPLACE INTO people VALUES(?,?,?,?)",
        (email, email.split("@")[0], "", dept),
    )


def _departments(conn):
    rows = [
        ("d_tp", "0", "技术平台部", "技术平台部", 30),
        ("d_base", "d_tp", "基础技术部", "技术平台部/基础技术部", 30),
        ("d_sec", "d_base", "安全组", "技术平台部/基础技术部/安全组", 30),
        ("d_market", "0", "市场与内容中心", "市场与内容中心", 5),
        ("d_hz", "0", "合作商", "合作商", 11),
        ("d_hzw", "d_hz", "W", "合作商/W", 9),
        ("d_vendor", "d_hzw", "某供应商(SP000083)", "合作商/W/某供应商(SP000083)", 9),
        ("d_hzv", "d_hz", "V", "合作商/V", 2),
        # V 父节点是递归汇总，真正允许加层的只有这条带真实部门叶子的行。
        ("d_v_sec", "d_hzv", "技术平台部-基础技术部-安全组",
         "合作商/V/技术平台部-基础技术部-安全组", 2),
    ]
    conn.executemany(
        "INSERT INTO departments(dept_id,parent_id,name,path,member_count) VALUES(?,?,?,?,?)",
        rows,
    )
    conn.commit()


def _consume_node_hc(conn):
    _reset_cache()
    headcount_map = dc._dept_headcount_map(conn)
    node_total = {}
    node_staff = {}
    if dc._dept_headcount_is_precounted(dc._dept_headcount_source_used()):
        for path, val in headcount_map.items():
            npath = dc._member_count_dept_key(path)[0] or dc._normalize_dept_path(path)
            if isinstance(val, (list, tuple)):
                total = int(val[0] or 0)
                staff = int(val[1] or 0) if len(val) > 1 else total
            else:
                total = staff = int(val or 0)
            node_total[npath] = max(node_total.get(npath, 0), total)
            node_staff[npath] = max(node_staff.get(npath, 0), staff)

        def _keep_root_sum(m):
            return sum(c for p, c in m.items()
                       if p != "Keep" and p.startswith("Keep/") and p.count("/") == 1)

        kt, ks = _keep_root_sum(node_total), _keep_root_sum(node_staff)
        if kt > 0:
            node_total["Keep"] = kt
        if ks > 0:
            node_staff["Keep"] = ks
    else:
        for path, cnt in headcount_map.items():
            cnt = int(cnt or 0) if not isinstance(cnt, (list, tuple)) else int((cnt or [0])[0] or 0)
            for anc in dc._ancestors(dc._normalize_dept_path(path)):
                node_total[anc] = node_total.get(anc, 0) + cnt
                node_staff[anc] = node_staff.get(anc, 0) + cnt
    return node_total, node_staff, dc._dept_headcount_source_used()


def _teams(conn):
    _reset_cache()
    captured = {}

    class Fake:
        def _send(self, code, obj):
            captured["code"] = code
            captured["obj"] = obj

    dc.H._teams(Fake(), conn, {})
    assert captured["code"] == 200
    return {t["dept"]: t for t in captured["obj"]["teams"]}


def main():
    _reset_cache()
    failures = []

    def check(name, got, want):
        ok = got == want
        print(("PASS" if ok else "FAIL"), name, "got=", got, "want=", want)
        if not ok:
            failures.append(name)

    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _departments(conn)
    _usage(conn, "employee@keep.com", "Keep/技术平台部/基础技术部/安全组", 100)
    _usage(conn, "vstaff@keep.com", "Keep/合作商/V/技术平台部-基础技术部-安全组", 80)
    _usage(conn, "vendor@keep.com", "Keep/合作商/W/某供应商(SP000083)", 60)
    _usage(conn, "market@keep.com", "Keep/市场与内容中心", 40)
    conn.commit()

    node_total, node_staff, source = _consume_node_hc(conn)
    check("口径=feishu_member_count", source, "feishu_member_count")
    check("V 叶子返回外部合作商键避免误入 max 通道",
          dc._member_count_dept_key("合作商/V/技术平台部-基础技术部-安全组"),
          (dc._CONTRACTOR_NODE, True))
    check("真实安全组 staff=正式30+V2",
          node_staff.get("Keep/技术平台部/基础技术部/安全组"), 32)
    check("真实安全组 total=正式30+V2",
          node_total.get("Keep/技术平台部/基础技术部/安全组"), 32)
    check("V 加到父部门基础技术部 staff",
          node_staff.get("Keep/技术平台部/基础技术部"), 32)
    check("V 加到顶层技术平台部 total",
          node_total.get("Keep/技术平台部"), 32)
    check("普通部门不受外包回归影响",
          (node_total.get("Keep/市场与内容中心"), node_staff.get("Keep/市场与内容中心")),
          (5, 5))
    check("W/SP 不进真实部门 staff/total",
          (node_total.get("Keep/技术平台部"), node_staff.get("Keep/技术平台部")),
          (32, 32))
    # V 已从外部合作商 total 搬出(11-2=9=仅 W 业务外包),搬移而非复制不双计;staff 恒 0。
    check("外部合作商搬出 V 后 total=9(仅 W),staff=0",
          (node_total.get("Keep/外部合作商"), node_staff.get("Keep/外部合作商")),
          (9, 0))

    teams = _teams(conn)
    sec = teams["Keep/技术平台部/基础技术部/安全组"]
    keep = teams["Keep"]
    check("部门榜安全组 headcount_staff=32", sec["headcount_staff"], 32)
    check("部门榜安全组 headcount_total=32", sec["headcount_total"], 32)
    check("Keep 根 staff 含 V 不含 W", keep["headcount_staff"], 37)
    check("Keep 活跃率不超过 100%",
          keep["active_rate"] <= 100.0, True)
    check("防双计:安全组 staff 精确等于 32", sec["headcount_staff"], 32)

    # --- 场景:部署前写的【旧缓存】(原始值、不含 V)命中时,返回仍要补 V(codex 评审#1) ---
    # V 加层在【返回时】套用而非写缓存时,故 6h 内命中旧缓存也立即生效,无需等过期。
    import json as _json, time as _time
    dc._dept_headcount_mem = None
    dc._dept_headcount_source_used_mem = None
    raw_no_v = {
        "Keep/技术平台部": [30, 30],
        "Keep/技术平台部/基础技术部": [30, 30],
        "Keep/技术平台部/基础技术部/安全组": [30, 30],
        "Keep/市场与内容中心": [5, 5],
        "Keep/外部合作商": [11, 0],
    }
    with open(dc._DEPT_HEADCOUNT_FILE, "w") as f:
        _json.dump({"ts": _time.time(), "source": dc._DEPT_HEADCOUNT_SOURCE,
                    "source_used": "feishu_member_count", "counts": raw_no_v},
                   f, ensure_ascii=False)
    stale_map = dc._dept_headcount_map(conn)
    check("旧缓存(无V)命中时返回仍补 V:安全组=[32,32]",
          stale_map.get("Keep/技术平台部/基础技术部/安全组"), [32, 32])
    check("旧缓存命中时外部合作商搬出 V:total=9 staff=0",
          stale_map.get("Keep/外部合作商"), [9, 0])

    _reset_cache()
    if failures:
        print("RESULT: FAIL ->", failures)
        raise AssertionError("dept active-rate V denominator failures: %s" % failures)
    print("RESULT: ALL PASS")


def test_dept_activerate_v_denominator():
    main()


def test_nested_v_member_count_no_double_count():
    """codex 评审#3:member_count 递归。V 子树有 "/" 三层嵌套(A 含 B 含 C)时,按自身净值
    (own=mc−【直接】子mc)加层 —— V 总数必须全算上(=A 的递归 mc),既不双计也不少计。"""
    _reset_cache()
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    rows = [
        ("d_tp", "0", "技术平台部", "技术平台部", 40),
        ("d_base", "d_tp", "基础技术部", "技术平台部/基础技术部", 30),
        ("d_sec", "d_base", "安全组", "技术平台部/基础技术部/安全组", 20),
        ("d_enc", "d_sec", "加密组", "技术平台部/基础技术部/安全组/加密组", 10),
        ("d_hz", "0", "合作商", "合作商", 9),
        ("d_hzw", "d_hz", "W", "合作商/W", 4),
        ("d_hzv", "d_hz", "V", "合作商/V", 5),
        # "/" 三层嵌套:A(递归5,含 B3,含 C1)。own: A=5-3=2, B=3-1=2, C=1 → 合计 5。
        ("d_vA", "d_hzv", "技术平台部-基础技术部",
         "合作商/V/技术平台部-基础技术部", 5),
        ("d_vB", "d_vA", "安全组",
         "合作商/V/技术平台部-基础技术部/安全组", 3),
        ("d_vC", "d_vB", "加密组",
         "合作商/V/技术平台部-基础技术部/安全组/加密组", 1),
    ]
    conn.executemany(
        "INSERT INTO departments(dept_id,parent_id,name,path,member_count) VALUES(?,?,?,?,?)",
        rows,
    )
    conn.commit()
    node_total, node_staff, source = _consume_node_hc(conn)
    assert source == "feishu_member_count"
    # 加密组 = 真实10 + C own1 = 11
    assert node_total.get("Keep/技术平台部/基础技术部/安全组/加密组") == 11, node_total
    # 安全组 = 真实20 + (B own2 + C own1) = 23(= V 在安全组下递归 3)
    assert node_total.get("Keep/技术平台部/基础技术部/安全组") == 23, node_total
    # 基础技术部 = 真实30 + (A2+B2+C1)=5 = 35(= V 在基础技术部下递归 5;非少计的 34)
    assert node_total.get("Keep/技术平台部/基础技术部") == 35, node_total
    # 技术平台部 = 真实40 + V总5 = 45
    assert node_total.get("Keep/技术平台部") == 45, node_total
    assert node_staff.get("Keep/技术平台部") == 45, node_staff
    # 外部合作商搬出 V 净总(2+2+1=5,等于 V 递归总数,不多不少):9-5=4(=W)。
    assert node_total.get("Keep/外部合作商") == 4, node_total
    _reset_cache()


def test_v_excludes_shared_leader_via_feishu_users():
    """飞书 member_count 含部门负责人(实证)。当 V 子部门的 leader 同时是真实部门的人时,
    若用 member_count 加层会把共享负责人多计一次。改用 feishu_users 枚举人头(不含挂名 leader),
    真实部门 staff = 真实 member_count + 枚举到的 V 人头。"""
    _reset_cache()
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.executescript(
        """
        CREATE TABLE feishu_users(
            open_id TEXT, user_id TEXT, union_id TEXT, email TEXT, name TEXT,
            dept_id TEXT, dept_path TEXT, status TEXT DEFAULT 'active',
            employee_type TEXT, updated_at TEXT DEFAULT ''
        );
        """
    )
    # 真实安全组 member_count=3(含 leader 李志杰,且 leader 作为成员被枚举);
    # V 安全组 member_count=2(含挂名 leader 李志杰),但实际 V 成员只有李沫延 1 人。
    conn.executemany(
        "INSERT INTO departments(dept_id,parent_id,name,path,member_count,leader_user_id) VALUES(?,?,?,?,?,?)",
        [
            ("d_tp", "0", "技术平台部", "技术平台部", 3, ""),
            ("d_base", "d_tp", "基础技术部", "技术平台部/基础技术部", 3, ""),
            ("d_sec", "d_base", "安全组", "技术平台部/基础技术部/安全组", 3, "ou_leader"),
            ("d_hz", "0", "合作商", "合作商", 2, ""),
            ("d_v", "d_hz", "V", "合作商/V", 2, ""),
            ("d_vsec", "d_v", "技术平台部-基础技术部-安全组",
             "合作商/V/技术平台部-基础技术部-安全组", 2, "ou_leader"),
        ],
    )
    # feishu_users:真实安全组枚举到 3 人(含 leader 李志杰);V 安全组只枚举到李沫延(leader 不入册)。
    conn.executemany(
        "INSERT INTO feishu_users(open_id,name,email,dept_path,status) VALUES(?,?,?,?,'active')",
        [
            ("ou_lidong", "李栋", "lidong@keep.com", "技术平台部/基础技术部/安全组"),
            ("ou_wangyang", "王杨", "wangyang02@keep.com", "技术平台部/基础技术部/安全组"),
            ("ou_leader", "李志杰", "lizhijie@keep.com", "技术平台部/基础技术部/安全组"),
            ("ou_limoyan", "李沫延", "limoyan_v@keep.com", "合作商/V/技术平台部-基础技术部-安全组"),
        ],
    )
    conn.commit()
    node_total, node_staff, source = _consume_node_hc(conn)
    assert source == "feishu_member_count", source
    # 安全组 staff = 真实 member_count 3 + 枚举到的 V 人头 1(李沫延) = 4。
    # 用 member_count(V=2,含挂名 leader)会得 5 —— 多计共享负责人。
    assert node_staff.get("Keep/技术平台部/基础技术部/安全组") == 4, node_staff
    assert node_total.get("Keep/技术平台部/基础技术部/安全组") == 4, node_total
    # 外部合作商搬出 1 名枚举 V:2-1=1。
    assert node_total.get("Keep/外部合作商") == 1, node_total
    _reset_cache()


if __name__ == "__main__":
    main()
    test_nested_v_member_count_no_double_count()
    test_v_excludes_shared_leader_via_feishu_users()
    print("ALL: PASS")
