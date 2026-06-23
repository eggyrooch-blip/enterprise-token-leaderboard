"""末级部门人员明细 /v1/team_members 回归测试。

来源:2026-06-23 孙可「部门榜到末级部门要显示人员:哪些人用了用了多少 token、哪些人没用」。
要点:
  * used(用了的人) 必须与部门榜「活跃集」同口径(同窗口/bucket/departed/scope),按 token 倒序;
  * unused(没用的人) = 花名册(people)在该子树、窗口内没用的人;
  * missing = member_count(递归含外包) − 已点到名人数 → 前端「另有 N 人未同步通讯录」,如实交代缺口;
  * 子部门递归归入;非本 scope 的部门负责人看不到;离职默认不出现。
"""
import importlib
import sqlite3
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import dev_collector  # noqa: E402

DEPT = "Keep/AI 平台事业部/AI 业务部/运动科学部"


def _schema(conn):
    conn.executescript(
        """
        CREATE TABLE usage(email TEXT, dept TEXT, period_type TEXT, period TEXT,
            source TEXT, client TEXT, total INTEGER, cost REAL, messages INTEGER,
            raw_dept TEXT, effective_dept TEXT, spend_bucket TEXT);
        CREATE TABLE people(email TEXT PRIMARY KEY, name TEXT, avatar TEXT, dept TEXT,
            raw_dept TEXT, effective_dept TEXT);
        CREATE TABLE feishu_member(email TEXT, name TEXT, dept TEXT, feature_key TEXT,
            credits REAL, usage_date TEXT, avatar TEXT, entity_id TEXT);
        CREATE TABLE departed(email TEXT PRIMARY KEY);
        """
    )


def _person(conn, email, name, dept, departed=False):
    conn.execute("INSERT OR REPLACE INTO people VALUES(?,?,?,?,?,?)",
                 (email, name, "", dept, dept, dept))
    if departed:
        conn.execute("INSERT OR REPLACE INTO departed VALUES(?)", (email,))


def _use(conn, email, dept, tokens, bucket="employee_staff_outsourcing", cost=1.0):
    conn.execute("INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (email, dept, "lifetime", "all", "subscription", "Claude Code",
                  tokens, cost, 0, dept, dept, bucket))


def _call(dc, conn, monkeypatch, qs=None, headcount=6, precounted=True, scope_user=None):
    monkeypatch.setattr(dc, "_dept_headcount_map", lambda *_a, **_k: {DEPT: headcount})
    monkeypatch.setattr(dc, "_dept_headcount_source_used",
                        lambda: dc._HEADCOUNT_SOURCE_PRECOUNTED if precounted else "people")
    captured = {}

    class Fake:
        _scope_user = scope_user

        def _send(self, code, obj):
            captured["code"] = code
            captured["obj"] = obj

    dc.H._team_members(Fake(), conn, qs or {"dept": [DEPT]})
    assert captured["code"] == 200, captured
    return captured["obj"]


@pytest.fixture()
def conn_5people():
    """5 人在 运动科学部:4 人用了(token 不同), 1 人(周啸天)没用。member_count=6 → missing=1。"""
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _person(conn, "wangheng01@keep.com", "王衡", DEPT)
    _person(conn, "liuzifang@keep.com", "刘自方", DEPT)
    _person(conn, "dongqingnan@keep.com", "董庆楠", DEPT)
    _person(conn, "yangfan01@keep.com", "杨帆", DEPT)
    _person(conn, "zhouxiaotian@keep.com", "周啸天", DEPT)   # 没用
    _use(conn, "wangheng01@keep.com", DEPT, 16_800_000_000)
    _use(conn, "liuzifang@keep.com", DEPT, 10_100_000_000)
    _use(conn, "dongqingnan@keep.com", DEPT, 12_100_000_000)
    _use(conn, "yangfan01@keep.com", DEPT, 373_000_000)
    return conn


def test_used_unused_missing(conn_5people, monkeypatch):
    dc = importlib.reload(dev_collector)
    obj = _call(dc, conn_5people, monkeypatch)
    assert obj["used_count"] == 4
    assert obj["unused_count"] == 1
    assert obj["unused"][0]["name"] == "周啸天"
    assert obj["missing"] == 1                      # member_count 6 − 已点名 5
    # used 按 token 倒序
    toks = [u["tokens"] for u in obj["used"]]
    assert toks == sorted(toks, reverse=True)
    assert obj["used"][0]["name"] == "王衡"
    assert obj["used"][0]["tokens"] == 16_800_000_000


def test_used_count_equals_active_denominator(conn_5people, monkeypatch):
    """used 人数必须等于部门榜该行的活跃人数(同口径)。"""
    dc = importlib.reload(dev_collector)
    monkeypatch.setattr(dc, "_dept_headcount_source_used",
                        lambda: dc._HEADCOUNT_SOURCE_PRECOUNTED)
    monkeypatch.setattr(dc, "_dept_headcount_map", lambda *_a, **_k: {DEPT: 6})
    cap = {}

    class Fake:
        _scope_user = None

        def _send(self, code, obj):
            cap[code] = obj

    dc.H._teams(Fake(), conn_5people, {})
    team = {t["dept"]: t for t in cap[200]["teams"]}[DEPT]
    obj = _call(dc, conn_5people, monkeypatch)
    assert obj["used_count"] == team["people"]      # 活跃集严格一致


def test_no_synthetic_completeness_when_roster_short(conn_5people, monkeypatch):
    """花名册短于 member_count 时,绝不假装完整 —— 用 missing 如实交代。"""
    dc = importlib.reload(dev_collector)
    obj = _call(dc, conn_5people, monkeypatch, headcount=6)
    assert obj["used_count"] + obj["unused_count"] + obj["missing"] == 6


def test_departed_excluded_by_default(monkeypatch):
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _person(conn, "a@keep.com", "A", DEPT)
    _person(conn, "gone@keep.com", "离职人", DEPT, departed=True)
    _use(conn, "a@keep.com", DEPT, 100)
    obj = _call(dc, conn, monkeypatch, headcount=2)
    names = [u["name"] for u in obj["used"]] + [u["name"] for u in obj["unused"]]
    assert "离职人" not in names


def test_child_dept_recursion(monkeypatch):
    """子部门的人计入父末级节点(member_count 递归口径)。"""
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    child = DEPT + "/算法组"
    _person(conn, "kid@keep.com", "小组员", child)
    _use(conn, "kid@keep.com", child, 500)
    _person(conn, "sib@keep.com", "隔壁部门", "Keep/技术平台部/安全组")
    _use(conn, "sib@keep.com", "Keep/技术平台部/安全组", 999)
    obj = _call(dc, conn, monkeypatch, headcount=1)
    assert [u["name"] for u in obj["used"]] == ["小组员"]   # 隔壁部门不混入


def test_scope_owner_other_dept_sees_nothing(conn_5people, monkeypatch):
    dc = importlib.reload(dev_collector)
    other = {"is_admin": False, "scope": "department",
             "owned_departments": ["Keep/技术平台部"], "email": "boss@keep.com"}
    obj = _call(dc, conn_5people, monkeypatch, scope_user=other)
    assert obj["used_count"] == 0 and obj["unused_count"] == 0
    # codex 评审:越权部门连 headcount/missing 也不能泄露
    assert obj["headcount_total"] is None and obj["missing"] == 0


def test_scope_owner_own_subtree_sees_members(conn_5people, monkeypatch):
    """部门负责人能看自己管辖子树的人员(运动科学部在 AI 平台事业部 下)。"""
    dc = importlib.reload(dev_collector)
    owner = {"is_admin": False, "scope": "department",
             "owned_departments": ["Keep/AI 平台事业部"], "email": "lead@keep.com"}
    obj = _call(dc, conn_5people, monkeypatch, scope_user=owner)
    assert obj["used_count"] == 4 and obj["headcount_total"] == 6


def test_multi_dept_user_token_split_matches_teams(monkeypatch):
    """跨模型评审(codex)发现的回归:一个人用量归属两个部门时,_team_members 必须像 _teams 一样
    按 (email, effective_dept, bucket) 分片各自归因 —— 目标叶子只计该子树内的那部分 token,
    不能把两部门的量合到一个叶子、又在另一个叶子让人消失。"""
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    other = "Keep/技术平台部/安全组"
    _person(conn, "x@keep.com", "跨部门X", DEPT)            # people 主部门记 DEPT
    _use(conn, "x@keep.com", DEPT, 200)                      # 200 归 运动科学部
    _use(conn, "x@keep.com", other, 100)                  # 100 归 安全组
    # 目标=运动科学部:X 只应显示 200(不是 300)
    obj_d = _call(dc, conn, monkeypatch, qs={"dept": [DEPT]}, headcount=1)
    used_d = {u["name"]: u["tokens"] for u in obj_d["used"]}
    assert used_d.get("跨部门X") == 200, obj_d["used"]
    # 目标=安全组:X 应出现且只显示 100
    monkeypatch.setattr(dc, "_dept_headcount_map", lambda *_a, **_k: {other: 1})
    cap = {}

    class Fake:
        _scope_user = None

        def _send(self, code, o):
            cap["o"] = o

    dc.H._team_members(Fake(), conn, {"dept": [other]})
    used_o = {u["name"]: u["tokens"] for u in cap["o"]["used"]}
    assert used_o.get("跨部门X") == 100, cap["o"]["used"]
    # 与 _teams 一致性:两个部门行都应把 X 计为活跃 1 人
    monkeypatch.setattr(dc, "_dept_headcount_source_used", lambda: dc._HEADCOUNT_SOURCE_PRECOUNTED)
    monkeypatch.setattr(dc, "_dept_headcount_map", lambda *_a, **_k: {DEPT: 1, other: 1})
    tcap = {}

    class FakeT:
        _scope_user = None

        def _send(self, code, o):
            tcap["o"] = o

    dc.H._teams(FakeT(), conn, {})
    teams = {t["dept"]: t for t in tcap["o"]["teams"]}
    assert teams[DEPT]["people"] == 1 and teams[other]["people"] == 1


def test_mid_dept_with_inactive_child_is_not_leaf(monkeypatch):
    """codex 评审第三轮:中层部门若有【零用量】子部门,/v1/teams 因只物化有用量部门会缺子行,
    绝不能因此把中层判成末级(否则前端在中层展开会暴露整棵子树花名册)。后端 is_leaf 须看组织结构。"""
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    parent = "Keep/AI 平台事业部/AI 业务部"
    # 直属 parent 的人有用量;子部门 运动科学部 零用量(无 usage 行)。
    _person(conn, "boss@keep.com", "业务主管", parent)
    _use(conn, "boss@keep.com", parent, 5000)
    # 组织结构(member_count 源)里 parent 下有子部门 运动科学部 → parent 非末级。
    monkeypatch.setattr(dc, "_dept_headcount_source_used", lambda: dc._HEADCOUNT_SOURCE_PRECOUNTED)
    monkeypatch.setattr(dc, "_dept_headcount_map",
                        lambda *_a, **_k: {"AI 平台事业部/AI 业务部": 6,
                                           "AI 平台事业部/AI 业务部/运动科学部": 6})
    cap = {}

    class Fake:
        _scope_user = None

        def _send(self, code, o):
            cap["o"] = o

    dc.H._teams(Fake(), conn, {})
    nodes = {t["dept"]: t for t in cap["o"]["teams"]}
    assert nodes[parent]["is_leaf"] is False     # 中层(有子部门)→ 非末级,前端不展开人员
    # 真叶子(无更深子部门)仍是末级
    leaf = parent + "/运动科学部"
    if leaf in nodes:
        assert nodes[leaf]["is_leaf"] is True


def test_real_leaf_is_marked_leaf(conn_5people, monkeypatch):
    """真末级部门 is_leaf=True(供前端允许展开人员)。"""
    dc = importlib.reload(dev_collector)
    monkeypatch.setattr(dc, "_dept_headcount_source_used", lambda: dc._HEADCOUNT_SOURCE_PRECOUNTED)
    monkeypatch.setattr(dc, "_dept_headcount_map", lambda *_a, **_k: {DEPT: 6})
    cap = {}

    class Fake:
        _scope_user = None

        def _send(self, code, o):
            cap["o"] = o

    dc.H._teams(Fake(), conn_5people, {})
    nodes = {t["dept"]: t for t in cap["o"]["teams"]}
    assert nodes[DEPT]["is_leaf"] is True
    assert nodes["Keep/AI 平台事业部"]["is_leaf"] is False   # 上层非末级


def test_aily_only_user_counts_as_used(monkeypatch):
    """只用了飞书 AI 权益(credits)、没 token 的人,也算活跃(used)。"""
    dc = importlib.reload(dev_collector)
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    _person(conn, "aily@keep.com", "权益用户", DEPT)
    conn.execute("INSERT INTO feishu_member VALUES(?,?,?,?,?,?,?,?)",
                 ("aily@keep.com", "权益用户", DEPT, "ai", 12.0, "2099-01-01", "", ""))
    obj = _call(dc, conn, monkeypatch, headcount=1)
    assert [u["name"] for u in obj["used"]] == ["权益用户"]
    assert obj["used"][0]["credits"] == 12.0
