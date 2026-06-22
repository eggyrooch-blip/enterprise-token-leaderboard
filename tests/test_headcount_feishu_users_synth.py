#!/usr/bin/env python3
"""合成自测 — headcount 接飞书全员表 feishu_users(无需真数据)。

当前生产库副本没有 feishu_users 表(bot 读用户权限未开),无法跑真数据验证。
本脚本用临时 sqlite 造 feishu_users 样例行,验证:
  1) feishu_users 为主源,叶子级分组计数正确;
  2) 消费点 _ancestors roll-up 后父部门 = 子孙累加;
  3) people 兜底:无 feishu_users 表时回退 people;
  4) 放行门:production_enablement_blocked=1 时飞书源被挡(返回 None,上层回退飞连)。

运行: python3 tests/test_headcount_feishu_users_synth.py
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.join(os.path.dirname(HERE), "collector")
sys.path.insert(0, COLLECTOR)

# dev_collector 在 import 期会读 DB 环境变量;给个不存在的临时路径即可,函数本身收 conn。
os.environ.setdefault("DEV_DB", os.path.join(HERE, "_synth_unused.db"))
import dev_collector as dc  # noqa: E402


def _mk_feishu_users(conn):
    conn.execute(
        """CREATE TABLE feishu_users(
            open_id TEXT PRIMARY KEY, user_id TEXT DEFAULT '',
            union_id TEXT DEFAULT '', email TEXT DEFAULT '', name TEXT NOT NULL,
            dept_id TEXT DEFAULT '', dept_path TEXT DEFAULT '',
            status TEXT DEFAULT 'active', employee_type TEXT DEFAULT '',
            updated_at TEXT)"""
    )
    # Keep 下两层:技术平台部/基础技术部/安全组(2人) + 技术平台部/基础技术部/固件组(3人)
    #          + 技术平台部(直属 1人)。再造离职 1人 + 无 dept_path 1人(都应被排除)。
    rows = [
        ("o1", "Keep/技术平台部/基础技术部/安全组", "active"),
        ("o2", "Keep/技术平台部/基础技术部/安全组", "active"),
        ("o3", "Keep/技术平台部/基础技术部/固件组", "active"),
        ("o4", "Keep/技术平台部/基础技术部/固件组", "active"),
        ("o5", "Keep/技术平台部/基础技术部/固件组", "active"),
        ("o6", "Keep/技术平台部", "active"),
        ("o7", "Keep/技术平台部/基础技术部/安全组", "departed"),  # 离职,排除
        ("o8", "", "active"),                                    # 无部门,排除
    ]
    conn.executemany(
        "INSERT INTO feishu_users(open_id,name,dept_path,status) VALUES(?,?,?,?)",
        [(oid, oid, dp, st) for (oid, dp, st) in rows],
    )
    conn.commit()


def _mk_people(conn):
    conn.execute(
        """CREATE TABLE people(email TEXT PRIMARY KEY, name TEXT, dept TEXT,
            effective_dept TEXT DEFAULT '', status TEXT DEFAULT 'active')"""
    )
    conn.executemany(
        "INSERT INTO people(email,name,dept,effective_dept,status) VALUES(?,?,?,?,?)",
        [("a@x", "a", "Keep/产品部", "", "active"),
         ("b@x", "b", "Keep/产品部", "", "active"),
         ("c@x", "c", "", "Keep/产品部/增长组", "active")],
    )
    conn.commit()


def _rollup(leaf_counts):
    """复刻 dev_collector 消费点(行~3091)的 roll-up:对每条叶子路径,把人数累加到其每级祖先。"""
    node_hc = {}
    for path, cnt in (leaf_counts or {}).items():
        for anc in dc._ancestors(dc._normalize_dept_path(path)):
            node_hc[anc] = node_hc.get(anc, 0) + (cnt or 0)
    return node_hc


def main():
    failures = []

    def check(name, got, want):
        ok = got == want
        print(("PASS" if ok else "FAIL"), name, "got=", got, "want=", want)
        if not ok:
            failures.append(name)

    # --- 场景1:feishu_users 为主源,叶子级计数 ---
    c1 = sqlite3.connect(":memory:")
    _mk_feishu_users(c1)
    leaf = dc._fetch_dept_headcount_feishu(c1)
    check("leaf 安全组=2", leaf.get("Keep/技术平台部/基础技术部/安全组"), 2)
    check("leaf 固件组=3", leaf.get("Keep/技术平台部/基础技术部/固件组"), 3)
    check("leaf 技术平台部(直属)=1", leaf.get("Keep/技术平台部"), 1)
    check("离职/无部门已排除(总叶子人数=6)", sum(leaf.values()), 6)

    # --- 场景2:消费点 roll-up,父=子孙累加 ---
    rolled = _rollup(leaf)
    # 技术平台部 = 直属1 + 基础技术部(安全2+固件3=5) = 6
    check("rollup 技术平台部=6", rolled.get("Keep/技术平台部"), 6)
    check("rollup 基础技术部=5", rolled.get("Keep/技术平台部/基础技术部"), 5)
    check("rollup Keep 根=6", rolled.get("Keep"), 6)

    # --- 场景3:无 feishu_users → 回退 people 子集 ---
    c2 = sqlite3.connect(":memory:")
    _mk_people(c2)
    leaf_p = dc._fetch_dept_headcount_feishu(c2)
    check("people 兜底 产品部叶子=2", leaf_p.get("Keep/产品部"), 2)
    check("people 兜底 增长组叶子=1", leaf_p.get("Keep/产品部/增长组"), 1)
    rolled_p = _rollup(leaf_p)
    check("people 兜底 rollup 产品部=3", rolled_p.get("Keep/产品部"), 3)

    # --- 场景4:放行门挡住 → 飞书源返回 None(上层回退飞连) ---
    c3 = sqlite3.connect(":memory:")
    _mk_feishu_users(c3)
    c3.execute("CREATE TABLE app_state(key TEXT PRIMARY KEY, value TEXT)")
    c3.execute("INSERT INTO app_state VALUES('feishu_directory_sync_production_enablement_blocked','1')")
    c3.commit()
    blocked = dc._fetch_dept_headcount_feishu(c3)
    check("放行门挡住→None", blocked, None)

    # 放行门=0 时不挡(用同库验证)
    c3.execute("UPDATE app_state SET value='0' WHERE key='feishu_directory_sync_production_enablement_blocked'")
    c3.commit()
    unblocked = dc._fetch_dept_headcount_feishu(c3)
    check("放行门=0→正常出数(安全组=2)", unblocked.get("Keep/技术平台部/基础技术部/安全组"), 2)

    print()
    if failures:
        print("RESULT: FAIL ->", failures)
        sys.exit(1)
    print("RESULT: ALL PASS")


if __name__ == "__main__":
    main()
