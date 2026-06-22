#!/usr/bin/env python3
"""合成自测 — headcount 接飞书部门 member_count(递归含子部门,无需真数据/凭证)。

当前生产库副本没有 member_count 数据(bot 读用户权限/sync 未跑),无法跑真数据验证。
本脚本用临时 sqlite 造 departments 表(member_count 是【父含子的递归值】),验证本次改动:

  1) member_count 为 headcount 主源,口径标记 = 'feishu_member_count';
  2) ⚠️核心:member_count 已是递归值 → 消费点【按 dept_path 直接查值】,绝不 _ancestors
     求和。证明父部门人数 = 飞书 member_count 原值,不会因 roll-up 把子部门再叠加而翻倍;
  3) 合成 'Keep' 顶级根 = 各顶层部门 member_count 之和 ≈ 造的总数(本例 1297),不是数千;
  4) 优先级:有 member_count 时压过 feishu_users(同库同时存在,仍取 member_count);
  5) 老库无 member_count 列/全 0 → 优雅回退 feishu_users(不崩)。

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


# 13 个顶层根部门 member_count(递归含各自所有子部门),合计 = 1297(对齐真实公司口径)。
# 实测真值参考:市场与内容中心=21、客户商业化中心=74。
_TOP_DEPTS = [
    ("市场与内容中心", 21),
    ("客户商业化中心", 74),
    ("技术平台部", 300),
    ("产品部", 180),
    ("用户增长中心", 160),
    ("运营中心", 150),
    ("供应链中心", 120),
    ("人力资源部", 90),
    ("财务部", 60),
    ("法务部", 40),
    ("行政部", 50),
    ("数据中心", 32),
    ("战略投资部", 20),
]
# 21+74+300+180+160+150+120+90+60+40+50+32+20 = 1297
_TOTAL = sum(c for _, c in _TOP_DEPTS)  # 1297


def _mk_departments(conn, with_member_count=True):
    """造 departments 表:13 个顶层根 + 技术平台部下两层子部门(子的 member_count 已递归,
    且 < 父值,证明直接查值时父不会把子再加进去)。"""
    mc_col = "member_count INTEGER DEFAULT 0," if with_member_count else ""
    conn.execute(
        """CREATE TABLE departments(
            dept_id TEXT PRIMARY KEY, open_dept_id TEXT DEFAULT '',
            parent_id TEXT, name TEXT NOT NULL, path TEXT NOT NULL,
            leader_user_id TEXT DEFAULT '', chat_id TEXT DEFAULT '',
            group_owner_user_id TEXT DEFAULT '', %s
            status TEXT DEFAULT 'active', updated_at TEXT)""" % mc_col
    )
    # path 用 build_department_paths 同口径 'Keep/<部门>/...'(_normalize_dept_path 幂等)。
    rows = []  # (dept_id, parent, name, path, member_count)
    for i, (name, cnt) in enumerate(_TOP_DEPTS, start=1):
        rows.append(("d%d" % i, "0", name, "Keep/" + name, cnt))
    # 技术平台部(300) 下:基础技术部(120,递归) → 安全组(30)、固件组(40)。
    rows.append(("d_jc", "d3", "基础技术部", "Keep/技术平台部/基础技术部", 120))
    rows.append(("d_aq", "d_jc", "安全组", "Keep/技术平台部/基础技术部/安全组", 30))
    rows.append(("d_gj", "d_jc", "固件组", "Keep/技术平台部/基础技术部/固件组", 40))

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


def _consume_node_hc(conn):
    """复刻 dev_collector 消费点(_teams)构建 node_hc 的逻辑,验证最终喂给部门榜的人数。
    与生产消费点同分支:member_count 口径直接查值 + Keep 根=顶层之和;否则 _ancestors roll-up。"""
    dc._dept_headcount_mem = None  # 清进程缓存,避免跨场景污染
    dc._dept_headcount_source_used_mem = None
    headcount_map = dc._dept_headcount_map(conn)
    node_hc = {}
    if dc._dept_headcount_is_precounted(dc._dept_headcount_source_used()):
        for path, cnt in headcount_map.items():
            node_hc[dc._normalize_dept_path(path)] = (cnt or 0)
        keep_total = sum(
            cnt for npath, cnt in node_hc.items()
            if npath != "Keep" and npath.startswith("Keep/") and npath.count("/") == 1)
        if keep_total > 0:
            node_hc["Keep"] = keep_total
    else:
        for path, cnt in headcount_map.items():
            for anc in dc._ancestors(dc._normalize_dept_path(path)):
                node_hc[anc] = node_hc.get(anc, 0) + (cnt or 0)
    return node_hc, dc._dept_headcount_source_used()


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

    # --- 场景1:member_count 为主源,口径标记正确 ---
    c1 = sqlite3.connect(":memory:")
    _mk_departments(c1, with_member_count=True)
    ret1 = dc._fetch_dept_headcount_feishu(c1)
    check("有 member_count→口径=feishu_member_count", ret1[1], "feishu_member_count")
    mc_map = ret1[0]
    check("member_count 市场与内容中心=21", mc_map.get("Keep/市场与内容中心"), 21)
    check("member_count 客户商业化中心=74", mc_map.get("Keep/客户商业化中心"), 74)

    # --- 场景2(核心):消费点直接查值,父不被子叠加翻倍 ---
    _reset_cache()
    node_hc, src = _consume_node_hc(c1)
    check("消费点口径=feishu_member_count", src, "feishu_member_count")
    # 技术平台部 member_count=300(已递归)。若错误 roll-up,会变成
    # 300 + 基础技术部120 + 安全组30 + 固件组40 = 490(翻倍)。直接查值必须是 300。
    check("技术平台部=300(非 roll-up 翻倍的 490)", node_hc.get("Keep/技术平台部"), 300)
    check("基础技术部=120(直接查值,非 120+30+40=190)",
          node_hc.get("Keep/技术平台部/基础技术部"), 120)
    check("安全组=30", node_hc.get("Keep/技术平台部/基础技术部/安全组"), 30)

    # --- 场景3:Keep 顶级根 = 13 顶层之和 ≈ 1297(非数千) ---
    check("Keep 顶级=1297(顶层 member_count 之和)", node_hc.get("Keep"), _TOTAL)
    assert _TOTAL == 1297, "测试数据总和应为 1297, 实为 %d" % _TOTAL
    # 防回归:绝不接近 roll-up 的虚高值。若误 roll-up,Keep 会 = 1297 + 子部门重复叠加 > 1297。
    check("Keep 不超过 1297(无 roll-up 重复计数)", node_hc.get("Keep") <= 1297, True)

    # --- 场景4:优先级 —— member_count 与 feishu_users 同库,仍取 member_count ---
    c4 = sqlite3.connect(":memory:")
    _mk_departments(c4, with_member_count=True)
    _mk_feishu_users(c4)  # 安全组叶子=2,若误用会让安全组=2 而非 member_count 的 30
    ret4 = dc._fetch_dept_headcount_feishu(c4)
    check("同库优先 member_count(口径)", ret4[1], "feishu_member_count")
    check("同库优先 member_count(安全组=30 非 feishu_users 的 2)",
          ret4[0].get("Keep/技术平台部/基础技术部/安全组"), 30)

    # --- 场景5:老库无 member_count 列 → 优雅回退 feishu_users(不崩) ---
    c5 = sqlite3.connect(":memory:")
    _mk_departments(c5, with_member_count=False)
    _mk_feishu_users(c5)
    ret5 = dc._fetch_dept_headcount_feishu(c5)
    check("无 member_count 列→回退 feishu(口径)", ret5[1], "feishu")
    check("回退后安全组=2(feishu_users 叶子计数)",
          ret5[0].get("Keep/技术平台部/基础技术部/安全组"), 2)

    # --- 场景6:member_count 全 0(生产库副本现状)→ 回退 feishu_users,不崩 ---
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
        sys.exit(1)
    print("RESULT: ALL PASS")


if __name__ == "__main__":
    main()
