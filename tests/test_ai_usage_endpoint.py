"""Regression for the unauthenticated AI usage endpoint GET /v1/ai/usage.

Hermes skill use case: 'sunke 过去一周的 token 用量' — per-person summary +
daily breakdown, login-name → email normalization, missing user is 0 (not 404),
departed default-excluded with a flag, and the no-user board drops departed +
agent rows + synthetic identities. Spins the real HTTP server and GETs it.

Dates are seeded relative to today so `?days=N` windows never rot.
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
    rows = [
        # alice: D(2)=100, D(1)=200+50(同天多行需 SUM), D(0)=300  → 合计 650
        ("alice@keep.com", "Keep/IT/脏部门", "day", D(2), "subscription", "Claude", "", "m", 100, 1.0),
        ("alice@keep.com", "Keep/IT/脏部门", "day", D(1), "subscription", "Codex", "", "m", 200, 2.0),
        ("alice@keep.com", "Keep/IT/脏部门", "day", D(1), "subscription", "Claude", "", "m2", 50, 0.5),
        ("alice@keep.com", "Keep/IT/脏部门", "day", D(0), "subscription", "Claude", "", "m", 300, 3.0),
        # 离职者 bob
        ("bob@keep.com", "Keep/IT", "day", D(0), "subscription", "Claude", "", "m", 999, 9.0),
        # agent key 用量 + 合成身份(litellm-key/-user): 都不进个人榜
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


def test_per_person_summary_and_daily(tmp_path, monkeypatch):
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        data = _get(server, "/v1/ai/usage?user=alice&days=30")
    finally:
        server.shutdown(); thread.join(timeout=3)
    assert data["user"] == "alice@keep.com"        # login-name → email
    assert data["name"] == "爱丽丝"
    assert data["dept"] == "Keep/IT/规范部门"        # canonical people.dept, not usage dept
    assert data["departed"] is False
    by_date = {d["date"]: d["total_tokens"] for d in data["daily"]}
    assert by_date == {D(2): 100, D(1): 250, D(0): 300}   # 同天多行被 SUM
    assert data["total_tokens"] == 650
    assert data["cost_usd"] == 6.5
    assert data["latest_usage_date"] == D(0)
    assert data["window"]["days"] == 30


def test_email_and_loginname_are_equivalent(tmp_path, monkeypatch):
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        a = _get(server, "/v1/ai/usage?user=alice&days=30")
        b = _get(server, "/v1/ai/usage?user=ALICE@keep.com&days=30")
    finally:
        server.shutdown(); thread.join(timeout=3)
    assert a["total_tokens"] == b["total_tokens"] == 650
    assert a["user"] == b["user"] == "alice@keep.com"


def test_unknown_user_is_zero_not_404(tmp_path, monkeypatch):
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        data = _get(server, "/v1/ai/usage?user=nobody&days=7")
    finally:
        server.shutdown(); thread.join(timeout=3)
    assert data["user"] == "nobody@keep.com"
    assert data["total_tokens"] == 0
    assert data["daily"] == []


def test_window_from_to_limits_to_single_day(tmp_path, monkeypatch):
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        data = _get(server, "/v1/ai/usage?user=alice&from=%s&to=%s" % (D(1), D(1)))
    finally:
        server.shutdown(); thread.join(timeout=3)
    assert data["total_tokens"] == 250
    assert [d["date"] for d in data["daily"]] == [D(1)]


def test_board_excludes_departed_and_synthetic_and_agent(tmp_path, monkeypatch):
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        board = _get(server, "/v1/ai/usage?days=30")
        with_dep = _get(server, "/v1/ai/usage?days=30&show_departed=1")
    finally:
        server.shutdown(); thread.join(timeout=3)
    emails = [r["user"] for r in board["ranking"]]
    assert "alice@keep.com" in emails and "bob@keep.com" not in emails
    # agent 行 + 合成身份绝不进个人榜(否则 5000/4000/3000 会把 alice 挤下榜首)
    assert not any(e.startswith("agent:") for e in emails)
    assert not any(e.startswith("litellm-key:") or e.startswith("litellm-user:") for e in emails)
    assert board["ranking"][0]["user"] == "alice@keep.com"
    assert board["ranking"][0]["dept"] == "Keep/IT/规范部门"   # canonical people.dept
    assert any(r["user"] == "bob@keep.com" for r in with_dep["ranking"])
    assert board["count"] == len(board["ranking"])


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
    assert shown["total_tokens"] == 999
