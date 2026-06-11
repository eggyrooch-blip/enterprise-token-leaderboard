"""未归类自愈 + 外部合作商收口 的回归测试。

来源:2026-06-11 孙可「未归类不应该自动化吗，进未归类前去飞连请求下」+「外部合作商不要平铺很乱」。
三个入库漏洞:
  1. LiteLLM 个人用户 dept 空(飞连查到却丢了 department_path)→ _resolve_feilian_info 带回 dept + write_db 补空。
  2. 飞书 aily 按 user_id 解析，查不到落裸组名 → load_feilian_map 加 email 索引，normalize 用合成 email 兜底。
  3. 外部供应商(合作商-W / 带 (SP码) / 裸公司名)平铺 → _normalize_dept_path 收口到 Keep/外部合作商/<公司>。
改动前这些断言会失败。
"""
import importlib
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))
sys.path.insert(0, str(ROOT / "collector" / "feishu"))


# ---------------------------------------------------------------------------
# 漏洞 3 — 外部供应商收口
# ---------------------------------------------------------------------------
@pytest.fixture()
def dc():
    import dev_collector
    return importlib.reload(dev_collector)


def test_external_supplier_W_collapses(dc):
    n = dc._normalize_dept_path
    assert n("Keep/合作商/W/北京再作品牌管理有限公司(SP000083)") == "Keep/外部合作商/北京再作品牌管理有限公司(SP000083)"
    assert n("Keep/合作商/W/中软国际科技服务有限公司(SP004867)") == "Keep/外部合作商/中软国际科技服务有限公司(SP004867)"


def test_external_supplier_bare_company_collapses(dc):
    # 裸公司名(带 SP码,无 合作商 前缀)也收口
    assert dc._normalize_dept_path("四川乔木禾电子商务有限公司(SP000442)") == "Keep/外部合作商/四川乔木禾电子商务有限公司(SP000442)"


def test_v_partner_still_folds_to_real_dept(dc):
    # 合作商-V(真实部门)逻辑不被外部收口误伤
    assert dc._normalize_dept_path("Keep/合作商/V/技术平台部-基础技术部-安全组") == "Keep/技术平台部/基础技术部/安全组"


def test_normal_path_idempotent(dc):
    for p in ("Keep/技术平台部/基础技术部/安全组", "Keep/CFO 线/法务部", "Keep"):
        assert dc._normalize_dept_path(p) == p


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


def _teams(dc, conn, monkeypatch, headcount=None):
    monkeypatch.setattr(dc, "_dept_headcount_map", lambda: dict(headcount or {}))
    cap = {}

    class Fake:
        def _send(self, code, obj): cap["obj"] = obj

    dc.H._teams(Fake(), conn, {})
    return {t["dept"]: t for t in cap["obj"]["teams"]}


def test_teams_collapses_multiple_suppliers_into_one_node(dc, monkeypatch):
    """两个不同供应商公司的 token → 收口到同一个 Keep/外部合作商 节点,不平铺成两个顶级节点。"""
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    def U(e, d, t): conn.execute("INSERT INTO usage VALUES(?,?,'lifetime','all','subscription','Claude Code',?,1,5)", (e, d, t))
    def P(e, d): conn.execute("INSERT OR REPLACE INTO people VALUES(?,?,?,?)", (e, e.split("@")[0], "", d))
    U("w1@keep.com", "Keep/合作商/W/中软国际科技服务有限公司(SP004867)", 100); P("w1@keep.com", "Keep/合作商/W/中软国际科技服务有限公司(SP004867)")
    U("w2@keep.com", "Keep/合作商/W/北京再作品牌管理有限公司(SP000083)", 200); P("w2@keep.com", "Keep/合作商/W/北京再作品牌管理有限公司(SP000083)")
    conn.commit()
    teams = _teams(dc, conn, monkeypatch, {})

    assert "Keep/外部合作商" in teams
    parent = teams["Keep/外部合作商"]
    assert parent["tokens"] == 300            # 两供应商汇总到一个父节点
    assert parent["people"] == 2
    # 各公司是其下钻子节点,不是顶级
    assert "Keep/外部合作商/中软国际科技服务有限公司(SP004867)" in teams
    assert "Keep/外部合作商/北京再作品牌管理有限公司(SP000083)" in teams
    # 没有把供应商摊成顶级 Keep 兄弟
    tops = [d for d in teams if d.count("/") == 0]
    assert tops == ["Keep"]


def test_bare_sp_aily_user_routes_to_external_not_uncategorized(dc, monkeypatch):
    """codex 评审发现:纯 aily 用户 feishu.dept 是裸供应商名(带 SP码、不以 Keep 开头)时，
    旧逻辑只在 startswith('Keep') 才归一 → 落未归类。应收口到 Keep/外部合作商，不落未归类。"""
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("INSERT INTO feishu_member VALUES('wb-x@keep.com','文洁',"
                 "'四川乔木禾电子商务有限公司(SP000442)','aily_credits',80,'2026-06-01','2026-06-07','')")
    conn.commit()  # 注意:无 people 行(纯 aily 外包)
    teams = _teams(dc, conn, monkeypatch, {})

    assert "Keep/未归类" not in teams, "裸 SP aily 外包不该落未归类"
    assert "Keep/外部合作商/四川乔木禾电子商务有限公司(SP000442)" in teams
    ext = teams["Keep/外部合作商"]
    assert ext["credits"] == 80
    assert ext["aily_people"] == 1


def test_token_user_bare_sp_dept_routes_to_external(dc, monkeypatch):
    """token 用户 usage.dept 为裸供应商名(扫描显示 usage.dept 里确有裸 SP 名)→ 也收口外部合作商。"""
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("INSERT INTO usage VALUES('w@keep.com','中软国际科技服务有限公司(SP004867)',"
                 "'lifetime','all','subscription','Claude Code',500,1,5)")
    # people.dept 也空,只能靠 usage.dept 归一
    conn.execute("INSERT INTO people VALUES('w@keep.com','w','','')")
    conn.commit()
    teams = _teams(dc, conn, monkeypatch, {})
    assert "Keep/未归类" not in teams
    assert teams["Keep/外部合作商"]["tokens"] == 500


# ---------------------------------------------------------------------------
# 漏洞 1 — LiteLLM dept 自愈
# ---------------------------------------------------------------------------
def test_litellm_resolve_returns_department_path(monkeypatch):
    import litellm_collector as L
    L = importlib.reload(L)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE people(email TEXT PRIMARY KEY, name TEXT, avatar TEXT, dept TEXT)")
    conn.execute("INSERT INTO people VALUES('api@keep.com','',' ','')")  # 已有行但 dept 空

    class FakeFC:
        def root_department_id(self): return "root"
        def user_by_email(self, e, root):
            return {"full_name": "接口用户", "avatar": "av", "department_path": "Keep/技术平台部/信息化技术部/信息化研发组"}

    monkeypatch.setattr(L, "_feilian", lambda: FakeFC())
    monkeypatch.setattr(L, "_FEILIAN_AVATARS", True)
    info = L._resolve_feilian_info(conn, {"api@keep.com"})
    name, avatar, dept = info["api@keep.com"]
    assert dept == "Keep/技术平台部/信息化技术部/信息化研发组"   # 旧版会丢掉这个


def test_litellm_write_db_fills_empty_dept(monkeypatch):
    import litellm_collector as L
    L = importlib.reload(L)
    monkeypatch.setattr(L, "DB", ":memory:")
    # 用真实 sqlite 文件连接以便 write_db 自己连
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    monkeypatch.setattr(L, "DB", path)

    class FakeFC:
        def root_department_id(self): return "root"
        def user_by_email(self, e, root):
            return {"full_name": "接口用户", "avatar": "av", "department_path": "Keep/CFO 线/财务中心/资金部"}

    monkeypatch.setattr(L, "_feilian", lambda: FakeFC())
    monkeypatch.setattr(L, "_FEILIAN_AVATARS", True)
    # rows: 16 列, 这里给一行最小 usage(email 在第 0 列)
    row = ("api@keep.com", "", "lifetime", "all", "litellm", "LiteLLM", "openai", "gpt-4o",
           1, 1, 0, 0, 0, 2, 0.01, 1)
    names = {"api@keep.com": ("接口用户", False)}   # (name, is_agent)
    L.write_db([row], names, {})
    conn = sqlite3.connect(path)
    dept = conn.execute("SELECT dept FROM people WHERE email='api@keep.com'").fetchone()[0]
    os.unlink(path)
    assert dept == "Keep/CFO 线/财务中心/资金部"


# ---------------------------------------------------------------------------
# 漏洞 2 — 飞书 aily email 兜底
# ---------------------------------------------------------------------------
def test_feishu_email_fallback_resolves_dept():
    import feishu_collector as F
    F = importlib.reload(F)
    # fmap 双索引:user_id(ou_*) 命中不了，但 email 索引命中
    rec = {"email": "luorui@keep.com", "name": "罗锐",
           "dept": "Keep/运动消费事业部/智能装备交付部/品控部/品质组", "avatar": "av"}
    fmap = {"luorui@keep.com": rec}   # 只有 email 索引(模拟 user_id 不匹配)
    captured = {"single": {}, "detail": [{"items": [{
        "entityInfo": {"externalID": "luorui", "entityName": "罗锐",
                       "entityExtraInfo": {"department": {"entityName": "品质组"}}},
        "featureUsageMap": {"aily_credits": 300},
    }]}]}
    F.EMAIL_DOMAIN = "keep.com"
    out = F.normalize(captured, "2026-06-01", fmap)
    m = out["members"][0]
    assert m["email"] == "luorui@keep.com"
    assert m["dept"] == "Keep/运动消费事业部/智能装备交付部/品控部/品质组"  # 不再是裸「品质组」
    assert m["name"] == "罗锐"


def test_feishu_no_match_keeps_bare_fallback():
    import feishu_collector as F
    F = importlib.reload(F)
    fmap = {}   # 飞连完全查不到
    captured = {"single": {}, "detail": [{"items": [{
        "entityInfo": {"externalID": "ghost", "entityName": "幽灵",
                       "entityExtraInfo": {"department": {"entityName": "某外部组"}}},
        "featureUsageMap": {"aily_credits": 5},
    }]}]}
    F.EMAIL_DOMAIN = "keep.com"
    out = F.normalize(captured, "2026-06-01", fmap)
    m = out["members"][0]
    assert m["dept"] == "某外部组"   # 查不到时退回飞书裸名(不崩)
