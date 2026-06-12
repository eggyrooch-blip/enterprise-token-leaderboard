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


def test_unresolved_aily_user_dropped_not_uncategorized(dc, monkeypatch):
    """孙可 2026-06-11:解析不到真实部门的人(离职/飞连外纯飞书用户,如罗锐@品质组)
    从部门榜跳过,不堆进 Keep/未归类。未归类节点应消失。"""
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("INSERT INTO feishu_member VALUES('luorui@keep.com','罗锐','品质组',"
                 "'aily_credits',300,'2026-06-01','2026-06-07','')")
    conn.commit()  # 裸非 SP 组名、无 people 行 → 解析失败
    teams = _teams(dc, conn, monkeypatch, {})
    assert all("未归类" not in d for d in teams)


def test_unresolved_token_user_dropped_not_uncategorized(dc, monkeypatch):
    """token 用户 people.dept 空且 usage.dept 裸别名(归不到 Keep)→ 跳过,不进未归类。"""
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("INSERT INTO usage VALUES('ghost@keep.com','某裸别名','lifetime','all',"
                 "'litellm','LiteLLM',500,1,5)")
    conn.execute("INSERT INTO people VALUES('ghost@keep.com','ghost','','')")
    conn.commit()
    teams = _teams(dc, conn, monkeypatch, {})
    assert "Keep/未归类" not in teams


def test_resolved_users_not_dropped(dc, monkeypatch):
    """正常 Keep 用户 + 外部供应商(SP码)不被误伤,照常出现;未归类不出现。"""
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("INSERT INTO usage VALUES('a@keep.com','Keep/CFO 线/法务部','lifetime','all','subscription','Claude Code',900,1,5)")
    conn.execute("INSERT OR REPLACE INTO people VALUES('a@keep.com','a','','Keep/CFO 线/法务部')")
    conn.execute("INSERT INTO feishu_member VALUES('wb@keep.com','文洁','四川乔木禾电子商务有限公司(SP000442)','aily_credits',80,'2026-06-01','2026-06-07','')")
    conn.commit()
    teams = _teams(dc, conn, monkeypatch, {})
    assert "Keep/CFO 线/法务部" in teams
    assert "Keep/外部合作商/四川乔木禾电子商务有限公司(SP000442)" in teams
    assert "Keep/未归类" not in teams


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
    captured = {"single": {}, "detail_by_day": {"2026-06-01": [{"items": [{
        "entityInfo": {"externalID": "luorui", "entityName": "罗锐",
                       "entityExtraInfo": {"department": {"entityName": "品质组"}}},
        "featureUsageMap": {"aily_credits": 300},
    }]}]}}
    F.EMAIL_DOMAIN = "keep.com"
    out = F.normalize(captured, ("2026-06-01", "2026-06-01"), fmap)
    m = out["members"][0]
    assert m["email"] == "luorui@keep.com"
    assert m["dept"] == "Keep/运动消费事业部/智能装备交付部/品控部/品质组"  # 不再是裸「品质组」
    assert m["name"] == "罗锐"
    assert m["usage_date"] == "2026-06-01"   # 按天:每行带 usage_date


def test_feishu_load_map_survives_pagination_failure(monkeypatch):
    """codex 评审:飞连分页中途网络失败,load_feilian_map 不能抛出崩掉采集器,
    应保留已载部分(或返回 None),让 normalize 退回飞书裸名。"""
    import feishu_collector as F
    F = importlib.reload(F)

    calls = {"n": 0}

    class FlakyFC:
        def root_department_id(self): return "root"
        def _request(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                # 第一页正常返回 100 人(凑满促使继续翻页)
                return {"user_list": [{"user_id": "ou_%d" % i, "email": "u%d@keep.com" % i,
                                       "full_name": "U%d" % i, "department_path": "Keep/A"}
                                      for i in range(100)]}
            raise RuntimeError("feilian 5xx mid-pagination")   # 第二页炸

    import types
    fake_mod = types.ModuleType("feilian_client")
    fake_mod.FeilianClient = lambda: FlakyFC()
    monkeypatch.setitem(sys.modules, "feilian_client", fake_mod)

    m = F.load_feilian_map()   # 不应抛异常
    assert m is not None and len(m) >= 100   # 保留了第一页已载部分(优雅降级)


def test_feishu_no_match_keeps_bare_fallback():
    import feishu_collector as F
    F = importlib.reload(F)
    fmap = {}   # 飞连完全查不到
    captured = {"single": {}, "detail_by_day": {"2026-06-01": [{"items": [{
        "entityInfo": {"externalID": "ghost", "entityName": "幽灵",
                       "entityExtraInfo": {"department": {"entityName": "某外部组"}}},
        "featureUsageMap": {"aily_credits": 5},
    }]}]}}
    F.EMAIL_DOMAIN = "keep.com"
    out = F.normalize(captured, ("2026-06-01", "2026-06-01"), fmap)
    m = out["members"][0]
    assert m["dept"] == "某外部组"   # 查不到时退回飞书裸名(不崩)


# ---------------------------------------------------------------------------
# 个人榜优先 people.dept(飞连全路径),不被 LiteLLM 裸别名经 MAX 盖掉
# ---------------------------------------------------------------------------
def _full_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE usage(email TEXT, dept TEXT, period_type TEXT, period TEXT,
            source TEXT, client TEXT, provider TEXT, model TEXT,
            input INT DEFAULT 0, output INT DEFAULT 0, cache_read INT DEFAULT 0,
            cache_write INT DEFAULT 0, reasoning INT DEFAULT 0,
            total INT, cost REAL, messages INT);
        CREATE TABLE people(email TEXT PRIMARY KEY, name TEXT, avatar TEXT, dept TEXT);
        CREATE TABLE departed(email TEXT PRIMARY KEY);
        CREATE TABLE report_log(serial TEXT, email TEXT, via TEXT, reported_at TEXT);
    """)
    return conn


def _usage_row(conn, email, dept, source, client, total):
    conn.execute("INSERT INTO usage(email,dept,period_type,period,source,client,provider,model,"
                 "input,output,total,cost,messages) VALUES(?,?,'lifetime','all',?,?,'x','m',?,0,?,1,5)",
                 (email, dept, source, client, total, total))


def _leaderboard(dc, conn):
    cap = {}
    class Fake:
        def _send(self, code, obj): cap["obj"] = obj
    dc.H._leaderboard(Fake(), conn, {})
    return {x["name"]: x for x in cap["obj"]["leaderboard"]}


def test_personal_board_prefers_people_dept_over_bare_litellm_alias(dc):
    """郭东霖:litellm 裸别名'技术平台部' + people.dept 飞连全路径。
    MAX(u.dept) 会因中文排在 'K' 之后误选裸别名;须优先 people.dept 全路径。"""
    conn = _full_conn()
    _usage_row(conn, "guo@keep.com", "技术平台部", "litellm", "LiteLLM", 3142)
    _usage_row(conn, "guo@keep.com", "Keep/技术平台部/推荐搜索部/算法组", "cursor", "Cursor", 100)
    conn.execute("INSERT INTO people VALUES('guo@keep.com','郭东霖','','Keep/技术平台部/推荐搜索部/算法组')")
    conn.commit()
    lb = _leaderboard(dc, conn)
    assert lb["郭东霖"]["dept"] == "Keep/技术平台部/推荐搜索部/算法组"


def test_personal_board_falls_back_to_usage_dept_when_no_people_path(dc):
    """people.dept 空(飞连未解析)→ 退回 usage.dept(裸别名),不崩。"""
    conn = _full_conn()
    _usage_row(conn, "bob@keep.com", "某团队别名", "litellm", "LiteLLM", 500)
    conn.execute("INSERT INTO people VALUES('bob@keep.com','Bob','','')")
    conn.commit()
    lb = _leaderboard(dc, conn)
    assert lb["Bob"]["dept"] == "某团队别名"


def _cursor_board(dc, conn):
    cap = {}
    class Fake:
        def _send(self, code, obj): cap["obj"] = obj
    dc.H._cursor(Fake(), conn, {})
    return {x["email"]: x for x in cap["obj"]["cursor"]}


def test_cursor_board_prefers_people_dept(dc):
    """Cursor 榜同样优先 people.dept(飞连全路径),不被裸 usage.dept 盖掉(codex 评审补的覆盖)。"""
    conn = _full_conn()
    _usage_row(conn, "guo@keep.com", "技术平台部", "cursor", "Cursor", 3000)
    conn.execute("INSERT INTO people VALUES('guo@keep.com','郭东霖','','Keep/技术平台部/推荐搜索部/算法组')")
    conn.commit()
    cb = _cursor_board(dc, conn)
    assert cb["guo@keep.com"]["dept"] == "Keep/技术平台部/推荐搜索部/算法组"
