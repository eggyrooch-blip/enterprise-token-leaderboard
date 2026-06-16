"""Regression for GET /v1/ai/usage — 口径完全对齐前端个人榜 /v1/leaderboard.

核心: 奇偶校验(parity) —— 同种子数据, ai/usage?user=X 的 total_tokens/cost_usd
必须 == /v1/leaderboard 中 X 那一行的 tokens/cost。这把两个接口的取数钉死成一套逻辑,
防止再次漂成两套(根因: 旧版 cost=SUM(所有 source) 把订阅牌价算进去, 千倍虚高)。

Dates 相对今天, 不腐烂。
"""
import datetime
import json
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import dev_collector  # noqa: E402

TODAY = datetime.date.today()


def D(n):
    return (TODAY - datetime.timedelta(days=n)).isoformat()


def _seed(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS people(email TEXT PRIMARY KEY, name TEXT, avatar TEXT, dept TEXT)")
    conn.execute("INSERT OR REPLACE INTO people(email,name,avatar,dept) VALUES(?,?,?,?)",
                 ("alice@keep.com", "爱丽丝", "", "Keep/IT/规范部门"))
    # 同一人混合来源: 订阅制(大额牌价 cost, 不进公司实付) + 网关实销(进 cost)
    rows = [
        # subscription: token 算数, 但 cost(牌价)绝不进 cost_usd
        ("alice@keep.com", "Keep/IT", "day", D(1), "subscription", "Claude", "", "m", 300, 999.0),
        # 网关实销 api/litellm: cost 进公司实付
        ("alice@keep.com", "Keep/IT", "day", D(1), "api", "LiteLLM", "", "m", 100, 2.5),
        ("alice@keep.com", "Keep/IT", "day", D(0), "litellm", "LiteLLM", "", "m", 50, 1.0),
        # 离职者 bob
        ("bob@keep.com", "Keep/IT", "day", D(0), "subscription", "Claude", "", "m", 777, 5.0),
        # agent + 合成身份: 都不进个人统计
        ("agent:botA", "unknown", "day", D(0), "litellm_agent", "LiteLLM", "", "m", 5000, 50.0),
        ("litellm-key:orphan", "unknown", "day", D(0), "litellm", "LiteLLM", "", "m", 4000, 40.0),
        ("litellm-user:ghost", "unknown", "day", D(0), "litellm", "LiteLLM", "", "m", 3000, 30.0),
    ]
    for (email, dept, pt, period, src, client, prov, model, total, cost) in rows:
        conn.execute(
            "INSERT OR REPLACE INTO usage(email,dept,period_type,period,source,client,provider,model,"
            "input,output,cache_read,cache_write,reasoning,total,cost,messages) "
            "VALUES(?,?,?,?,?,?,?,?,0,0,0,0,0,?,?,0)",
            (email, dept, pt, period, src, client, prov, model, total, cost))
    # 飞书权益点(1点=1token, 点成本并入 cost) —— alice 当天 12 点
    conn.execute(
        "INSERT OR REPLACE INTO feishu_member(email,name,dept,feature_key,credits,usage_date,avatar,entity_id) "
        "VALUES('alice@keep.com','爱丽丝','Keep/IT/规范部门','AI_credits',12.0,?,'','')", (D(0),))
    # 订阅: alice codex $25/月(无界席位), 个人榜 cost 按窗口摊销叠加 —— 评审发现的关键路径。
    conn.execute(
        "CREATE TABLE IF NOT EXISTS subscriptions(email TEXT, tool TEXT, seat INTEGER DEFAULT 1, "
        "tier TEXT DEFAULT 'standard', monthly_fee_usd REAL DEFAULT 0, display_name TEXT DEFAULT '', "
        "dept TEXT DEFAULT '', start_date TEXT, end_date TEXT, synced_at TEXT, PRIMARY KEY(email,tool,seat))")
    conn.execute(
        "INSERT OR REPLACE INTO subscriptions(email,tool,seat,tier,monthly_fee_usd,synced_at) "
        "VALUES('alice@keep.com','codex',1,'standard',25.0,?)", (D(0),))
    conn.execute("CREATE TABLE IF NOT EXISTS departed(email TEXT PRIMARY KEY, reason TEXT, marked_at TEXT)")
    conn.execute("INSERT OR REPLACE INTO departed(email,reason,marked_at) VALUES('bob@keep.com','left',?)", (D(30),))
    conn.commit()


def _get(server, path):
    url = "http://127.0.0.1:%d%s" % (server.server_address[1], path)
    return json.loads(urllib.request.urlopen(url, timeout=10).read().decode("utf-8"))


def _serve(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_collector, "DB", str(tmp_path / "tok.db"))
    monkeypatch.setattr(dev_collector, "AI_EMAIL_DOMAIN", "keep.com")
    conn = dev_collector.db()
    _seed(conn)
    conn.close()
    server = dev_collector.ThreadingHTTPServer(("127.0.0.1", 0), dev_collector.H)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    return server, thread


def test_parity_single_user_matches_leaderboard_row(tmp_path, monkeypatch):
    """奇偶校验: ai/usage?user=alice 的 tokens/cost == /v1/leaderboard 中 alice 行。"""
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        ai = _get(server, "/v1/ai/usage?user=alice&days=30")
        lb = _get(server, "/v1/leaderboard?days=30")
    finally:
        server.shutdown(); thread.join(timeout=3)
    alice = next(r for r in lb["leaderboard"] if r["email"] == "alice@keep.com")
    # token 含飞书点: 300+100+50 + 12 = 462
    assert ai["total_tokens"] == alice["tokens"] == 462
    # cost = 网关实销(3.5) + 飞书点成本 + 订阅费窗口摊销 —— 必须与前端那一行分毫一致
    assert abs(ai["cost_usd"] - alice["cost"]) < 0.001
    assert ai["cost_usd"] < 100   # 含 $25 订阅摊销但远小于订阅牌价 999, 证明牌价没被算进去


def test_cost_excludes_subscription_per_token_notional(tmp_path, monkeypatch):
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        ai = _get(server, "/v1/ai/usage?user=alice&days=30")
    finally:
        server.shutdown(); thread.join(timeout=3)
    # 含网关 3.5 + 飞书点 + 订阅 $25 摊销(~25), 但绝不含订阅制按 token 的牌价 999。
    assert 3.5 <= ai["cost_usd"] < 100


def test_daily_tokens_sum_to_total(tmp_path, monkeypatch):
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        ai = _get(server, "/v1/ai/usage?user=alice&days=30")
    finally:
        server.shutdown(); thread.join(timeout=3)
    # daily 只给 token(cost 含订阅月费不可按天拆); token 各天之和 == total。
    assert sum(d["total_tokens"] for d in ai["daily"]) == ai["total_tokens"]
    assert all("cost_usd" not in d for d in ai["daily"])


def test_email_and_loginname_are_equivalent(tmp_path, monkeypatch):
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        a = _get(server, "/v1/ai/usage?user=alice&days=30")
        b = _get(server, "/v1/ai/usage?user=ALICE@keep.com&days=30")
    finally:
        server.shutdown(); thread.join(timeout=3)
    assert a["total_tokens"] == b["total_tokens"] == 462
    assert a["user"] == b["user"] == "alice@keep.com"
    assert a["dept"] == "Keep/IT/规范部门"


def test_unknown_user_is_zero_not_404(tmp_path, monkeypatch):
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        data = _get(server, "/v1/ai/usage?user=nobody&days=7")
    finally:
        server.shutdown(); thread.join(timeout=3)
    assert data["user"] == "nobody@keep.com"
    assert data["total_tokens"] == 0 and data["daily"] == []


def test_board_excludes_departed_synthetic_agent_and_cost_not_inflated(tmp_path, monkeypatch):
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        board = _get(server, "/v1/ai/usage?days=30")
        with_dep = _get(server, "/v1/ai/usage?days=30&show_departed=1")
    finally:
        server.shutdown(); thread.join(timeout=3)
    emails = [r["user"] for r in board["ranking"]]
    assert "alice@keep.com" in emails and "bob@keep.com" not in emails
    assert not any(e.startswith("agent:") for e in emails)
    assert not any(e.startswith("litellm-key:") or e.startswith("litellm-user:") for e in emails)
    alice = next(r for r in board["ranking"] if r["user"] == "alice@keep.com")
    assert alice["total_tokens"] == 462
    assert alice["cost_usd"] < 100       # 含订阅摊销但牌价 999 没被算进 board
    assert any(r["user"] == "bob@keep.com" for r in with_dep["ranking"])


def test_days_window_matches_leaderboard_with_future_row(tmp_path, monkeypatch):
    """评审 #4: ?days=N 在两接口须是同一窗口(无人为 to 上界)。

    种一条 future-dated(明天) day 行: leaderboard?days=7 无上界会纳入它,
    ai/usage?days=7 也必须纳入(否则窗口漂)。断言两接口对 alice 的 total 相等。
    """
    def seed_future(conn):
        _seed(conn)
        conn.execute(
            "INSERT OR REPLACE INTO usage(email,dept,period_type,period,source,client,provider,model,"
            "input,output,cache_read,cache_write,reasoning,total,cost,messages) "
            "VALUES('alice@keep.com','Keep/IT','day',?,'litellm','LiteLLM','','m',0,0,0,0,0,7,0.1,0)",
            (D(-1),))  # 明天
        conn.commit()
    monkeypatch.setattr(dev_collector, "DB", str(tmp_path / "tok.db"))
    monkeypatch.setattr(dev_collector, "AI_EMAIL_DOMAIN", "keep.com")
    conn = dev_collector.db(); seed_future(conn); conn.close()
    server = dev_collector.ThreadingHTTPServer(("127.0.0.1", 0), dev_collector.H)
    thread = threading.Thread(target=server.serve_forever); thread.daemon = True; thread.start()
    try:
        ai = _get(server, "/v1/ai/usage?user=alice&days=7")
        lb = _get(server, "/v1/leaderboard?days=7")
    finally:
        server.shutdown(); thread.join(timeout=3)
    alice = next(r for r in lb["leaderboard"] if r["email"] == "alice@keep.com")
    assert ai["total_tokens"] == alice["tokens"]            # 同一窗口 → 相等
    assert ai["total_tokens"] == sum(d["total_tokens"] for d in ai["daily"])


def test_case_duplicate_email_total_equals_daily(tmp_path, monkeypatch):
    """评审 #4: DB 里同一人有大小写不同的 email 行时, total 与 daily 不能分裂。"""
    def seed_dup(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS people(email TEXT PRIMARY KEY, name TEXT, avatar TEXT, dept TEXT)")
        for em, tot in [("Alice@keep.com", 100), ("alice@keep.com", 50)]:
            conn.execute(
                "INSERT OR REPLACE INTO usage(email,dept,period_type,period,source,client,provider,model,"
                "input,output,cache_read,cache_write,reasoning,total,cost,messages) "
                "VALUES(?,?,'day',?,'litellm','LiteLLM','','m',0,0,0,0,0,?,0.1,0)",
                (em, "Keep/IT", D(0), tot))
        conn.commit()
    monkeypatch.setattr(dev_collector, "DB", str(tmp_path / "tok.db"))
    monkeypatch.setattr(dev_collector, "AI_EMAIL_DOMAIN", "keep.com")
    conn = dev_collector.db(); seed_dup(conn); conn.close()
    server = dev_collector.ThreadingHTTPServer(("127.0.0.1", 0), dev_collector.H)
    thread = threading.Thread(target=server.serve_forever); thread.daemon = True; thread.start()
    try:
        ai = _get(server, "/v1/ai/usage?user=alice&days=7")
        lb = _get(server, "/v1/leaderboard?days=7")
    finally:
        server.shutdown(); thread.join(timeout=3)
    # 两条大小写不同的行都归到同一人: total=150, 且 == daily 之和(不分裂)。
    assert ai["total_tokens"] == 150
    assert sum(d["total_tokens"] for d in ai["daily"]) == ai["total_tokens"]
    # 评审 #5: 个人榜也合并大小写变体 → 只有一行 150, 与 ai/usage 分毫一致(不是 100/50 两行)。
    alice_rows = [r for r in lb["leaderboard"] if (r["email"] or "").lower() == "alice@keep.com"]
    assert len(alice_rows) == 1
    assert alice_rows[0]["tokens"] == ai["total_tokens"] == 150


def test_single_user_departed_default_zero_with_flag(tmp_path, monkeypatch):
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        default = _get(server, "/v1/ai/usage?user=bob&days=30")
        shown = _get(server, "/v1/ai/usage?user=bob&days=30&show_departed=1")
    finally:
        server.shutdown(); thread.join(timeout=3)
    assert default["departed"] is True
    assert default["total_tokens"] == 0 and default["daily"] == []
    assert shown["departed"] is True
    assert shown["total_tokens"] == 777
