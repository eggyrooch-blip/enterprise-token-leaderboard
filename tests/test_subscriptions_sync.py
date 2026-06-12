import pathlib
import sqlite3
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import subscriptions_sync  # noqa: E402


def test_parse_roster_rows_skips_headers_and_detects_claude_premium():
    codex_rows = [
        ["忽略", "Codex 账号名", "注册邮箱", "", "", "飞书实名", "部门完整路径"],
        ["", "alice-codex", [{"text": "alice@gmail.com", "type": "url"}], "", "", [{"text": "Alice", "type": "text"}], "Keep/平台/基础"],
        ["", "skip-me", "", "", "", "Nobody", "Keep/平台/基础"],
    ]
    claude_rows = [
        ["", "", "邮箱前缀", "注册邮箱", "飞书 user_id", "飞书实名", "部门", "", "备注"],
        ["", "", "alice", "alice@gmail.com", [{"text": "ou_123", "type": "text"}], "Alice", "Keep/平台/基础", "", [{"text": "Premium 席位", "type": "text"}]],
        ["", "", "bob", "bob@gmail.com", "", "Bob", "Keep/平台/基础", "", ""],
        ["", "", "carol", "carol@gmail.com", "ou_456", "Carol", "Keep/平台/基础", "", ""],
    ]
    cursor_rows = [
        ["姓名", "邮箱"],
        [[{"text": "Dora", "type": "text"}], [{"text": "dora@keep.com", "type": "url"}]],
        ["Empty", ""],
    ]

    codex = subscriptions_sync.parse_codex_rows(codex_rows)
    claude = subscriptions_sync.parse_claude_rows(claude_rows)
    cursor = subscriptions_sync.parse_direct_email_rows(cursor_rows, "cursor", 40.0)

    assert codex == [{
        "tool": "codex",
        "display_name": "Alice",
        "raw_email": "alice@gmail.com",
        "dept": "Keep/平台/基础",
        "tier": "standard",
        "monthly_fee_usd": 25.0,
    }]
    assert claude == [
        {
            "tool": "claude",
            "display_name": "Alice",
            "raw_email": "ou_123@keep.com",
            "dept": "Keep/平台/基础",
            "tier": "premium",
            "monthly_fee_usd": 100.0,
        },
        {
            "tool": "claude",
            "display_name": "Carol",
            "raw_email": "ou_456@keep.com",
            "dept": "Keep/平台/基础",
            "tier": "standard",
            "monthly_fee_usd": 25.0,
        },
    ]
    assert cursor == [{
        "tool": "cursor",
        "display_name": "Dora",
        "raw_email": "dora@keep.com",
        "dept": "",
        "tier": "standard",
        "monthly_fee_usd": 40.0,
    }]


def test_resolve_codex_identity_handles_direct_unique_ambiguous_and_missing():
    people = [
        {"email": "direct@keep.com", "name": "Direct", "dept": "Keep/平台/基础"},
        {"email": "alice@keep.com", "name": "Alice", "dept": "Keep/平台/基础"},
        {"email": "sam-a@keep.com", "name": "Sam", "dept": "Keep/平台/A组"},
        {"email": "sam-b@keep.com", "name": "Sam", "dept": "Keep/平台/B组"},
    ]
    people_index = subscriptions_sync.index_people(people)

    direct, unresolved = subscriptions_sync.resolve_codex_identity({
        "tool": "codex",
        "display_name": "Direct",
        "raw_email": "direct@keep.com",
        "dept": "Keep/平台/基础",
        "tier": "standard",
        "monthly_fee_usd": 25.0,
    }, people_index)
    assert direct["email"] == "direct@keep.com"
    assert unresolved is None

    matched, unresolved = subscriptions_sync.resolve_codex_identity({
        "tool": "codex",
        "display_name": "Alice",
        "raw_email": "alice@gmail.com",
        "dept": "Keep/平台/基础",
        "tier": "standard",
        "monthly_fee_usd": 25.0,
    }, people_index)
    assert matched["email"] == "alice@keep.com"
    assert unresolved is None

    matched, unresolved = subscriptions_sync.resolve_codex_identity({
        "tool": "codex",
        "display_name": "Sam",
        "raw_email": "sam@gmail.com",
        "dept": "Keep/平台",
        "tier": "standard",
        "monthly_fee_usd": 25.0,
    }, people_index)
    assert matched is None
    assert unresolved["reason"] == "ambiguous"

    matched, unresolved = subscriptions_sync.resolve_codex_identity({
        "tool": "codex",
        "display_name": "Nobody",
        "raw_email": "nobody@gmail.com",
        "dept": "Keep/平台/基础",
        "tier": "standard",
        "monthly_fee_usd": 25.0,
    }, people_index)
    assert matched is None
    assert unresolved["reason"] == "no_match"


def test_resolve_codex_identity_prefers_keep_email_over_ambiguous_name_match():
    people_index = subscriptions_sync.index_people([
        {"email": "zhangbo01@keep.com", "name": "张博", "dept": "Keep/平台/A组"},
        {"email": "zhangbo02@keep.com", "name": "张博", "dept": "Keep/平台/B组"},
    ])

    matched, unresolved = subscriptions_sync.resolve_codex_identity({
        "tool": "codex",
        "display_name": "张博",
        "raw_email": "zhangbo04@keep.com",
        "dept": "Keep/平台/C组",
        "tier": "standard",
        "monthly_fee_usd": 25.0,
    }, people_index)

    assert unresolved is None
    assert matched["email"] == "zhangbo04@keep.com"


def test_resolve_codex_identity_strips_parenthetical_name_but_preserves_unresolved_display():
    people_index = subscriptions_sync.index_people([
        {"email": "wuziao@keep.com", "name": "吴子遨", "dept": "Keep/平台/基础"},
    ])

    matched, unresolved = subscriptions_sync.resolve_codex_identity({
        "tool": "codex",
        "display_name": "吴子遨（同 row 20 第二账号）",
        "raw_email": "wuziao+2@gmail.com",
        "dept": "Keep/平台/基础",
        "tier": "standard",
        "monthly_fee_usd": 25.0,
    }, people_index)
    assert unresolved is None
    assert matched["email"] == "wuziao@keep.com"

    matched, unresolved = subscriptions_sync.resolve_codex_identity({
        "tool": "codex",
        "display_name": "王楠(暂定)",
        "raw_email": "wangnan@gmail.com",
        "dept": "Keep/平台/基础",
        "tier": "standard",
        "monthly_fee_usd": 25.0,
    }, people_index)
    assert matched is None
    assert unresolved["reason"] == "no_match"
    assert unresolved["display_name"] == "王楠(暂定)"


def test_write_snapshot_full_replace_is_idempotent_and_removes_deleted_people():
    conn = sqlite3.connect(":memory:")
    subscriptions_sync.ensure_tables(conn)

    subs = [
        {
            "email": "alice@keep.com",
            "tool": "codex",
            "tier": "standard",
            "monthly_fee_usd": 25.0,
            "seats": 1,
            "display_name": "Alice",
            "dept": "Keep/平台/基础",
        },
        {
            "email": "bob@keep.com",
            "tool": "claude",
            "tier": "premium",
            "monthly_fee_usd": 100.0,
            "seats": 1,
            "display_name": "Bob",
            "dept": "Keep/平台/应用",
        },
    ]
    unresolved = [{
        "tool": "codex",
        "display_name": "Ghost",
        "raw_email": "ghost@gmail.com",
        "dept": "Keep/平台/基础",
        "reason": "no_match",
    }]

    subscriptions_sync.write_snapshot(conn, subs, unresolved, "2026-06-12T10:00:00Z")
    subscriptions_sync.write_snapshot(conn, subs, unresolved, "2026-06-12T10:00:00Z")
    rows = conn.execute(
        "SELECT email, tool, tier, monthly_fee_usd FROM subscriptions ORDER BY email, tool"
    ).fetchall()
    unresolved_rows = conn.execute(
        "SELECT tool, display_name, raw_email, reason FROM subscriptions_unresolved"
    ).fetchall()

    assert rows == [
        ("alice@keep.com", "codex", "standard", 25.0),
        ("bob@keep.com", "claude", "premium", 100.0),
    ]
    assert unresolved_rows == [("codex", "Ghost", "ghost@gmail.com", "no_match")]

    subscriptions_sync.write_snapshot(conn, subs[:1], [], "2026-06-13T10:00:00Z")
    rows = conn.execute(
        "SELECT email, tool FROM subscriptions ORDER BY email, tool"
    ).fetchall()
    unresolved_count = conn.execute(
        "SELECT COUNT(*) FROM subscriptions_unresolved"
    ).fetchone()[0]

    assert rows == [("alice@keep.com", "codex")]
    assert unresolved_count == 0


def test_build_snapshot_aggregates_seats_per_email_and_tool():
    people = [
        {"email": "alice@keep.com", "name": "Alice", "dept": "Keep/平台/基础"},
    ]
    rows_by_tool = {
        "codex": [
            {
                "tool": "codex",
                "display_name": "Alice",
                "raw_email": "alice+1@gmail.com",
                "dept": "Keep/平台/基础",
                "tier": "standard",
                "monthly_fee_usd": 25.0,
            },
            {
                "tool": "codex",
                "display_name": "Alice",
                "raw_email": "alice+2@gmail.com",
                "dept": "Keep/平台/基础",
                "tier": "standard",
                "monthly_fee_usd": 25.0,
            },
        ],
        "claude": [
            {
                "tool": "claude",
                "display_name": "Alice",
                "raw_email": "alice@keep.com",
                "dept": "Keep/平台/基础",
                "tier": "standard",
                "monthly_fee_usd": 25.0,
            },
            {
                "tool": "claude",
                "display_name": "Alice",
                "raw_email": "alice@keep.com",
                "dept": "Keep/平台/基础",
                "tier": "premium",
                "monthly_fee_usd": 100.0,
            },
        ],
        "cursor": [],
        "windsurf": [],
    }

    subs, unresolved = subscriptions_sync._build_snapshot(rows_by_tool, people)

    assert unresolved == []
    assert subs == [
        {
            "email": "alice@keep.com",
            "tool": "claude",
            "tier": "premium",
            "monthly_fee_usd": 125.0,
            "seats": 2,
            "display_name": "Alice",
            "dept": "Keep/平台/基础",
        },
        {
            "email": "alice@keep.com",
            "tool": "codex",
            "tier": "standard",
            "monthly_fee_usd": 50.0,
            "seats": 2,
            "display_name": "Alice",
            "dept": "Keep/平台/基础",
        },
    ]
