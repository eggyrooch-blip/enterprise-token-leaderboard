"""部门榜：业务外包归并 + aily 并入 的回归测试。

需求(2026-06-11 与余光灿对话):
  1. 外包飞连路径 'Keep/合作商/<供应商>/真实部门-子部门-组' 要折回真实 Keep 树节点，
     不再堆在 'Keep/合作商' 子树 → _normalize_dept_path。
  2. aily(飞书 AI 权益, 单位「点」)的人并入部门榜：新增人均点数, 活跃渗透取 token∪aily 并集,
     点数永不与 token 加总, aily 取最新一个周期快照。
这些断言在改动前会失败(旧 _teams 无 credits/per_capita_credits, 外包堆在合作商节点)。
"""
import importlib
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))


@pytest.fixture()
def dc():
    import dev_collector
    return importlib.reload(dev_collector)


def _norm(dc):
    return dc._normalize_dept_path


def test_normalize_outsource_path_folds_into_real_dept(dc):
    n = _norm(dc)
    assert n("Keep/合作商/V/技术平台部-基础技术部-安全组") == "Keep/技术平台部/基础技术部/安全组"
    assert n("Keep/合作商/V/客户商业化中心-商业产品部-产品技术组") == "Keep/客户商业化中心/商业产品部/产品技术组"


def test_normalize_is_idempotent_for_non_outsource(dc):
    n = _norm(dc)
    for p in ("Keep/技术平台部/基础技术部/安全组", "Keep/技术平台部", "Keep", ""):
        assert n(p) == p
    assert n(None) is None


def _schema(conn):
    conn.executescript(
        """
        CREATE TABLE usage(email TEXT, dept TEXT, period_type TEXT, period TEXT,
            source TEXT, client TEXT, total INTEGER, cost REAL, messages INTEGER);
        CREATE TABLE people(email TEXT PRIMARY KEY, name TEXT, avatar TEXT, dept TEXT);
        CREATE TABLE feishu_member(email TEXT, name TEXT, dept TEXT, feature_key TEXT,
            credits REAL, period_start TEXT, period_end TEXT, avatar TEXT);
        CREATE TABLE departed(email TEXT PRIMARY KEY);
        """
    )


def _usage(conn, email, dept, total):
    conn.execute("INSERT INTO usage VALUES(?,?,'lifetime','all','subscription','Claude Code',?,1,5)",
                 (email, dept, total))


def _person(conn, email, dept):
    conn.execute("INSERT OR REPLACE INTO people VALUES(?,?,?,?)", (email, email.split("@")[0], "", dept))


def _aily(conn, email, dept, credits, period="2026-06-01", feature="aily_credits"):
    conn.execute("INSERT INTO feishu_member VALUES(?,?,?,?,?,?,?,?)",
                 (email, email.split("@")[0], dept, feature, credits, period, "2026-06-07", ""))


def _teams(dc, conn, monkeypatch, headcount=None):
    """直接调用 H._teams，捕获 payload。headcount 注入避免触网。"""
    monkeypatch.setattr(dc, "_dept_headcount_map", lambda: dict(headcount or {}))
    captured = {}

    class Fake:
        def _send(self, code, obj):
            captured["code"] = code
            captured["obj"] = obj

    dc.H._teams(Fake(), conn, {})
    return {t["dept"]: t for t in captured["obj"]["teams"]}


def test_outsource_merges_into_real_dept_node(dc, monkeypatch):
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    # 外包 + 正式员工同在安全组
    _usage(conn, "waibao@keep.com", "Keep/合作商/V/技术平台部-基础技术部-安全组", 1_000_000)
    _person(conn, "waibao@keep.com", "Keep/合作商/V/技术平台部-基础技术部-安全组")
    _usage(conn, "zhengshi@keep.com", "Keep/技术平台部/基础技术部/安全组", 500_000)
    _person(conn, "zhengshi@keep.com", "Keep/技术平台部/基础技术部/安全组")
    conn.commit()
    teams = _teams(dc, conn, monkeypatch, {"Keep/技术平台部/基础技术部/安全组": 2})

    assert "Keep/合作商" not in teams, "外包不该再堆在合作商节点"
    sec = teams["Keep/技术平台部/基础技术部/安全组"]
    assert sec["tokens"] == 1_500_000
    assert sec["token_people"] == 2
    assert sec["per_capita_tokens"] == 750_000
    assert sec["active_rate"] == 100.0  # 2 活跃 / 2 在职


def test_aily_only_person_appears_in_dept_with_credits(dc, monkeypatch):
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _aily(conn, "ailyonly@keep.com", "Keep/客户商业化中心/商业产品部/产品技术组", 2000)
    _person(conn, "ailyonly@keep.com", "Keep/客户商业化中心/商业产品部/产品技术组")
    conn.commit()
    teams = _teams(dc, conn, monkeypatch, {"Keep/客户商业化中心/商业产品部/产品技术组": 3})

    grp = teams["Keep/客户商业化中心/商业产品部/产品技术组"]
    assert grp["credits"] == 2000.0
    assert grp["per_capita_credits"] == 2000.0
    assert grp["aily_people"] == 1
    assert grp["token_people"] == 0
    assert grp["people"] == 1            # 活跃渗透并集纳入纯 aily 用户
    assert grp["tokens"] == 0            # 点数绝不并入 token


def test_active_penetration_is_union_token_and_aily(dc, monkeypatch):
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    # 同部门：一人用 token，另一人只用 aily → 活跃人数=2(并集)
    _usage(conn, "coder@keep.com", "Keep/A/组", 800_000)
    _person(conn, "coder@keep.com", "Keep/A/组")
    _aily(conn, "ailyer@keep.com", "Keep/A/组", 1500)
    _person(conn, "ailyer@keep.com", "Keep/A/组")
    conn.commit()
    teams = _teams(dc, conn, monkeypatch, {"Keep/A/组": 4})

    grp = teams["Keep/A/组"]
    assert grp["token_people"] == 1
    assert grp["aily_people"] == 1
    assert grp["people"] == 2                       # token ∪ aily
    assert grp["active_rate"] == 50.0               # 2 / 4
    assert grp["per_capita_tokens"] == 800_000      # 人均 token 不被 aily-only 稀释
    assert grp["per_capita_credits"] == 1500.0


def test_stale_headcount_cache_with_raw_outsource_path_still_resolves(dc, monkeypatch):
    """codex 评审发现:dept_headcount.json 6h 缓存里旧路径仍是 'Keep/合作商/V/...',
    命中时不重算。_teams 消费点必须再归一化一次,否则归并后的真实叶子拿不到 headcount。"""
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _usage(conn, "waibao@keep.com", "Keep/合作商/V/技术平台部-基础技术部-安全组", 1_000_000)
    _person(conn, "waibao@keep.com", "Keep/合作商/V/技术平台部-基础技术部-安全组")
    conn.commit()
    # 模拟“改动前写入的旧缓存”:headcount 仍带未归并的合作商路径
    teams = _teams(dc, conn, monkeypatch, {"Keep/合作商/V/技术平台部-基础技术部-安全组": 1})

    sec = teams["Keep/技术平台部/基础技术部/安全组"]
    assert sec["headcount"] == 1, "旧缓存的合作商 headcount 必须归并到真实叶子"
    assert sec["active_rate"] == 100.0


def test_credits_count_all_feishu_features_not_only_aily(dc, monkeypatch):
    """孙可 2026-06-11 定:人均点数 = 飞书全部点数(AI 通用额度 + aily),不是只 aily 那一类。
    锁死该口径,防止后续被收窄成只 feature_key='aily_credits'。"""
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _aily(conn, "u@keep.com", "Keep/C/组", 700, feature="AI_credits")
    _aily(conn, "u@keep.com", "Keep/C/组", 300, feature="aily_credits")
    _person(conn, "u@keep.com", "Keep/C/组")
    conn.commit()
    teams = _teams(dc, conn, monkeypatch, {"Keep/C/组": 2})

    grp = teams["Keep/C/组"]
    assert grp["credits"] == 1000.0          # 700 + 300,两类都算
    assert grp["per_capita_credits"] == 1000.0
    assert grp["aily_people"] == 1


def test_aily_uses_latest_period_snapshot_only(dc, monkeypatch):
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _aily(conn, "x@keep.com", "Keep/B/组", 999, period="2026-05-01")   # 旧周期
    _aily(conn, "x@keep.com", "Keep/B/组", 1200, period="2026-06-01")  # 最新周期
    _person(conn, "x@keep.com", "Keep/B/组")
    conn.commit()
    teams = _teams(dc, conn, monkeypatch, {})

    assert teams["Keep/B/组"]["credits"] == 1200.0  # 只取最新周期, 不累加旧周期
