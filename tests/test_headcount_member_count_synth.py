#!/usr/bin/env python3
"""合成自测 — headcount 接飞书部门 member_count(递归含子部门,无需真数据/凭证)。

本脚本用临时 sqlite 造 departments 表(member_count 是【父含子的递归值】),验证:

  1) member_count 为 headcount 主源,口径标记 = 'feishu_member_count';
  2) ⚠️核心:member_count 已是递归值 → 消费点【按 dept_path 直接查值】,绝不 _ancestors
     求和。证明父部门人数 = 飞书 member_count 原值,不会因 roll-up 把子部门再叠加而翻倍;
  3) ⚠️命名空间修复:真实飞书 departments.path 是【裸】路径(无 Keep 前缀,如 '技术平台部'),
     消费点必须补 'Keep/' 前缀才能与人员侧('Keep/技术平台部')对上,否则 lookup 全 miss;
  4) ⚠️合作商修复:'合作商' 子树(整棵 W/V)是外部合作商,绝不能折进真实部门或顶成 Keep 根;
     统一收口到单一 'Keep/外部合作商' 节点,只计【含外包】不计【员工】;
  5) 双口径:每部门 [total, staff];Keep 根 total=全量、staff=排除合作商;
  6) 合成 'Keep' 顶级根 = 各顶层节点之和;
  7) 优先级:有 member_count 时压过 feishu_users;老库无列/全 0 → 优雅回退 feishu_users。

运行: python3 tests/test_headcount_member_count_synth.py
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.join(os.path.dirname(HERE), "collector")
sys.path.insert(0, COLLECTOR)

os.environ.setdefault("DEV_DB", os.path.join(HERE, "_synth_unused_mc.db"))
import dev_collector as dc  # noqa: E402


# 顶层根部门 member_count(递归含各自所有子部门)。员工侧(不含合作商)合计 = 690;
# 加上 外部合作商(合作商,607)→ 含外包合计 = 1297(对齐真实公司口径)。
# 真实飞书 departments.path 是【裸】路径(无 Keep 前缀)—— 这里故意用裸路径覆盖命名空间修复。
_STAFF_DEPTS = [
    ("市场与内容中心", 21),
    ("客户商业化中心", 74),
    ("技术平台部", 300),
    ("产品部", 180),
    ("用户增长中心", 95),
    ("运营中心", 20),
]
_STAFF_TOTAL = sum(c for _, c in _STAFF_DEPTS)  # 690
_CONTRACTOR_TOTAL = 607                          # 合作商(含外包,不计员工)
_GRAND_TOTAL = _STAFF_TOTAL + _CONTRACTOR_TOTAL  # 1297


def _mk_departments(conn, with_member_count=True):
    """造 departments 表:裸路径顶层根 + 技术平台部子树 + 合作商(W/V)外包子树。"""
    mc_col = "member_count INTEGER DEFAULT 0," if with_member_count else ""
    conn.execute(
        """CREATE TABLE departments(
            dept_id TEXT PRIMARY KEY, open_dept_id TEXT DEFAULT '',
            parent_id TEXT, name TEXT NOT NULL, path TEXT NOT NULL,
            leader_user_id TEXT DEFAULT '', chat_id TEXT DEFAULT '',
            group_owner_user_id TEXT DEFAULT '', %s
            status TEXT DEFAULT 'active', updated_at TEXT)""" % mc_col
    )
    rows = []  # (dept_id, parent, name, path, member_count)
    for i, (name, cnt) in enumerate(_STAFF_DEPTS, start=1):
        rows.append(("d%d" % i, "0", name, name, cnt))  # 裸路径(无 Keep 前缀)
    # 技术平台部(300) 下:基础技术部(120,递归) → 安全组(30)、固件组(40)。裸路径。
    rows.append(("d_jc", "d3", "基础技术部", "技术平台部/基础技术部", 120))
    rows.append(("d_aq", "d_jc", "安全组", "技术平台部/基础技术部/安全组", 30))
    rows.append(("d_gj", "d_jc", "固件组", "技术平台部/基础技术部/固件组", 40))
    # 合作商外包子树:整棵收口到 外部合作商,只计含外包。
    rows.append(("d_hz", "0", "合作商", "合作商", _CONTRACTOR_TOTAL))
    rows.append(("d_hzw", "d_hz", "W", "合作商/W", 494))
    rows.append(("d_hzwc", "d_hzw", "某供应商(SP000442)",
                 "合作商/W/某供应商(SP000442)", 30))
    rows.append(("d_hzv", "d_hz", "V", "合作商/V", 94))
    # 合作商/V 下的业务外包(叶子短横命名,真实数据里会回折真实部门)—— 仍属外包,不进员工。
    rows.append(("d_hzvx", "d_hzv", "技术平台部-基础技术部-安全组",
                 "合作商/V/技术平台部-基础技术部-安全组", 2))

    if with_member_count:
        conn.executemany(
            "INSERT INTO departments(dept_id,parent_id,name,path,member_count)"
            " VALUES(?,?,?,?,?)",
            [(did, par, nm, pth, mc) for (did, par, nm, pth, mc) in rows],
        )
    else:
        conn.executemany(
            "INSERT INTO departments(dept_id,parent_id,name,path) VALUES(?,?,?,?)",
            [(did, par, nm, pth) for (did, par, nm, pth, _mc) in rows],
        )
    conn.commit()


def _mk_feishu_users(conn):
    """用于「优先级 / 回退」场景:member_count 缺失时才用它。叶子级行计数。"""
    conn.execute(
        """CREATE TABLE feishu_users(
            open_id TEXT PRIMARY KEY, name TEXT NOT NULL,
            dept_path TEXT DEFAULT '', status TEXT DEFAULT 'active')"""
    )
    conn.executemany(
        "INSERT INTO feishu_users(open_id,name,dept_path,status) VALUES(?,?,?,?)",
        [("u1", "u1", "Keep/技术平台部/基础技术部/安全组", "active"),
         ("u2", "u2", "Keep/技术平台部/基础技术部/安全组", "active")],
    )
    conn.commit()


def _val(slot, idx):
    """map 值统一取:新口径 [total, staff] 取 idx;老标量两口径相等。"""
    if isinstance(slot, (list, tuple)):
        return slot[idx] if idx < len(slot) else slot[0]
    return slot


def _consume_node_hc(conn):
    """复刻 dev_collector 消费点(_teams)双口径 node_hc 构建,验证最终喂给部门榜的人数。"""
    dc._dept_headcount_mem = None
    dc._dept_headcount_source_used_mem = None
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

        def _ksum(m):
            return sum(c for p, c in m.items()
                       if p != "Keep" and p.startswith("Keep/") and p.count("/") == 1)
        kt, ks = _ksum(node_total), _ksum(node_staff)
        if kt > 0:
            node_total["Keep"] = kt
        if ks > 0:
            node_staff["Keep"] = ks
    else:
        for path, cnt in headcount_map.items():
            cnt = int(_val(cnt, 0) or 0)
            for anc in dc._ancestors(dc._normalize_dept_path(path)):
                node_total[anc] = node_total.get(anc, 0) + cnt
                node_staff[anc] = node_staff.get(anc, 0) + cnt
    return node_total, node_staff, dc._dept_headcount_source_used()


def _reset_cache():
    dc._dept_headcount_mem = None
    dc._dept_headcount_source_used_mem = None
    try:
        if os.path.exists(dc._DEPT_HEADCOUNT_FILE):
            os.remove(dc._DEPT_HEADCOUNT_FILE)
    except Exception:
        pass


def main():
    _reset_cache()
    failures = []

    def check(name, got, want):
        ok = got == want
        print(("PASS" if ok else "FAIL"), name, "got=", got, "want=", want)
        if not ok:
            failures.append(name)

    # --- 场景1:member_count 为主源,口径标记 + 命名空间补前缀 ---
    c1 = sqlite3.connect(":memory:")
    _mk_departments(c1, with_member_count=True)
    ret1 = dc._fetch_dept_headcount_feishu(c1)
    check("有 member_count→口径=feishu_member_count", ret1[1], "feishu_member_count")
    mc_map = ret1[0]
    # ⚠️命名空间:裸 '市场与内容中心' 必须补 'Keep/' 前缀(否则人员侧对不上)。
    check("裸路径补 Keep 前缀:市场与内容中心=[21,21]",
          mc_map.get("Keep/市场与内容中心"), [21, 21])
    check("裸路径无前缀键不存在(确认已补全)",
          "市场与内容中心" in mc_map, False)
    # ⚠️合作商:整棵收口到 外部合作商,total=607,staff=0;绝不映成 Keep 根。
    check("合作商收口外部合作商 total=607 staff=0",
          mc_map.get("Keep/外部合作商"), [607, 0])
    check("合作商不污染 Keep 根键", "Keep" in mc_map, False)
    # 合作商/V 业务外包也归外部合作商(不折回真实安全组,避免双计)。
    check("合作商/V 业务外包不折回真实部门",
          mc_map.get("Keep/技术平台部/基础技术部/安全组"), [30, 30])  # 仅真实安全组 30,无 +2

    # --- 场景2(核心):消费点直接查值,父不被子叠加翻倍 ---
    _reset_cache()
    node_total, node_staff, src = _consume_node_hc(c1)
    check("消费点口径=feishu_member_count", src, "feishu_member_count")
    check("技术平台部 total=300(非 roll-up 翻倍的 490)",
          node_total.get("Keep/技术平台部"), 300)
    check("技术平台部 staff=300(真实部门 staff=total)",
          node_staff.get("Keep/技术平台部"), 300)
    check("基础技术部=120(直接查值,非 190)",
          node_total.get("Keep/技术平台部/基础技术部"), 120)
    check("安全组=30", node_total.get("Keep/技术平台部/基础技术部/安全组"), 30)

    # --- 场景3:双口径 Keep 根 —— total=1297(含外包) / staff=690(员工) ---
    check("Keep 含外包 total=1297", node_total.get("Keep"), _GRAND_TOTAL)
    check("Keep 员工 staff=690(排除外部合作商)", node_staff.get("Keep"), _STAFF_TOTAL)
    assert _GRAND_TOTAL == 1297 and _STAFF_TOTAL == 690
    check("Keep total 不超 1297(无 roll-up 重复)", node_total.get("Keep") <= 1297, True)
    check("外部合作商 total=607 staff=0",
          (node_total.get("Keep/外部合作商"), node_staff.get("Keep/外部合作商")),
          (607, 0))

    # --- 场景4:优先级 —— member_count 与 feishu_users 同库,仍取 member_count ---
    c4 = sqlite3.connect(":memory:")
    _mk_departments(c4, with_member_count=True)
    _mk_feishu_users(c4)
    ret4 = dc._fetch_dept_headcount_feishu(c4)
    check("同库优先 member_count(口径)", ret4[1], "feishu_member_count")
    check("同库优先 member_count(安全组=[30,30] 非 feishu_users 的 2)",
          ret4[0].get("Keep/技术平台部/基础技术部/安全组"), [30, 30])

    # --- 场景5:老库无 member_count 列 → 优雅回退 feishu_users(不崩) ---
    c5 = sqlite3.connect(":memory:")
    _mk_departments(c5, with_member_count=False)
    _mk_feishu_users(c5)
    ret5 = dc._fetch_dept_headcount_feishu(c5)
    check("无 member_count 列→回退 feishu(口径)", ret5[1], "feishu")
    check("回退后安全组=2(feishu_users 叶子计数)",
          ret5[0].get("Keep/技术平台部/基础技术部/安全组"), 2)

    # --- 场景6:member_count 全 0 → 回退 feishu_users,不崩 ---
    c6 = sqlite3.connect(":memory:")
    _mk_departments(c6, with_member_count=True)
    c6.execute("UPDATE departments SET member_count=0")
    c6.commit()
    _mk_feishu_users(c6)
    ret6 = dc._fetch_dept_headcount_feishu(c6)
    check("member_count 全 0→回退 feishu(口径)", ret6[1], "feishu")
    check("全 0 回退后安全组=2",
          ret6[0].get("Keep/技术平台部/基础技术部/安全组"), 2)

    _reset_cache()
    print()
    if failures:
        print("RESULT: FAIL ->", failures)
        raise AssertionError("headcount member_count synth failures: %s" % failures)
    print("RESULT: ALL PASS")


def test_headcount_member_count_synth():
    """pytest 入口:让 TEST 门(pytest)也能收集并跑核心 SPEC 契约,而非仅脚本。"""
    main()


if __name__ == "__main__":
    main()
