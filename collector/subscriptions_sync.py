#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步 Feishu 表格里的订阅名单到本地 SQLite。

目标环境：CentOS7 / Python 3.6.8 / SQLite 3.7.17 / 纯标准库。
"""
from __future__ import print_function

import datetime
import json
import os
import sqlite3
import sys

try:
    from urllib.request import Request, urlopen
    from urllib.parse import quote
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover
    from urllib2 import Request, urlopen, HTTPError, URLError  # type: ignore
    from urllib import quote  # type: ignore


DB = os.environ.get("DEV_DB", "/tmp/tok.db")
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
FEISHU_HOST = os.environ.get("FEISHU_HOST", "https://open.feishu.cn").rstrip("/")
TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "60"))
SPREADSHEET_TOKEN = "WuK7sLkIthIn2Htrz2BcIiipnEb"
SHEETS = {
    "codex": "aGseou",
    "claude": "6SIHS",
    "cursor": "KvJN7D",
    "windsurf": "fl4xUJ",
}


def _die(msg, code):
    sys.stderr.write("subscriptions_sync: %s\n" % msg)
    sys.exit(code)


def _cell(row, idx):
    if idx >= len(row):
        return ""
    val = row[idx]
    if val is None:
        return ""
    if isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, dict):
                text = item.get("text")
                parts.append("" if text is None else str(text))
            else:
                parts.append(str(item))
        return "".join(parts).strip()
    return str(val).strip()


def _match_name(text):
    name = (text or "").strip()
    if not name:
        return ""
    cut = len(name)
    for marker in ("（", "("):
        idx = name.find(marker)
        if idx >= 0 and idx < cut:
            cut = idx
    return name[:cut].strip()


def parse_codex_rows(rows):
    out = []
    for i, row in enumerate(rows or []):
        if i == 0:
            continue
        raw_email = _cell(row, 2)
        if not raw_email:
            continue
        out.append({
            "tool": "codex",
            "display_name": _cell(row, 5),
            "raw_email": raw_email,
            "dept": _cell(row, 6),
            "tier": "standard",
            "monthly_fee_usd": 25.0,
        })
    return out


def parse_claude_rows(rows):
    out = []
    for i, row in enumerate(rows or []):
        if i == 0:
            continue
        user_id = _cell(row, 4)
        if not user_id:
            continue
        remark = _cell(row, 8)
        premium = "Premium 席位" in remark
        out.append({
            "tool": "claude",
            "display_name": _cell(row, 5),
            "raw_email": user_id + "@keep.com",
            "dept": _cell(row, 6),
            "tier": "premium" if premium else "standard",
            "monthly_fee_usd": 100.0 if premium else 25.0,
        })
    return out


def parse_direct_email_rows(rows, tool, fee):
    out = []
    for i, row in enumerate(rows or []):
        if i == 0:
            continue
        raw_email = _cell(row, 1)
        if not raw_email:
            continue
        out.append({
            "tool": tool,
            "display_name": _cell(row, 0),
            "raw_email": raw_email,
            "dept": "",
            "tier": "standard",
            "monthly_fee_usd": float(fee),
        })
    return out


def index_people(people):
    by_name = {}
    for person in people or []:
        name = _match_name(person.get("name") or "")
        if not name:
            continue
        by_name.setdefault(name, []).append({
            "email": (person.get("email") or "").strip(),
            "name": name,
            "dept": (person.get("dept") or "").strip(),
        })
    return by_name


def _resolved_member(member, email):
    item = dict(member)
    item["email"] = email
    return item


def _unresolved_member(member, reason):
    return {
        "tool": member.get("tool") or "",
        "display_name": member.get("display_name") or "",
        "raw_email": member.get("raw_email") or "",
        "dept": member.get("dept") or "",
        "reason": reason,
    }


def resolve_codex_identity(member, people_index):
    raw_email = (member.get("raw_email") or "").strip()
    if raw_email.endswith("@keep.com"):
        return _resolved_member(member, raw_email), None
    display_name = _match_name(member.get("display_name") or "")
    candidates = list((people_index or {}).get(display_name) or [])
    if not candidates:
        return None, _unresolved_member(member, "no_match")
    if len(candidates) == 1:
        return _resolved_member(member, candidates[0]["email"]), None
    dept = (member.get("dept") or "").strip()
    if dept:
        dept_matches = [c for c in candidates if (c.get("dept") or "").strip() == dept]
        if len(dept_matches) == 1:
            return _resolved_member(member, dept_matches[0]["email"]), None
    return None, _unresolved_member(member, "ambiguous")


def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions(
            email TEXT NOT NULL,
            tool TEXT NOT NULL,
            tier TEXT NOT NULL DEFAULT 'standard',
            monthly_fee_usd REAL NOT NULL DEFAULT 0,
            seats INTEGER NOT NULL DEFAULT 1,
            display_name TEXT DEFAULT '',
            dept TEXT DEFAULT '',
            synced_at TEXT NOT NULL,
            PRIMARY KEY(email, tool)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions_unresolved(
            tool TEXT NOT NULL,
            display_name TEXT DEFAULT '',
            raw_email TEXT DEFAULT '',
            dept TEXT DEFAULT '',
            reason TEXT NOT NULL,
            synced_at TEXT NOT NULL
        )
    """)
    sub_cols = [row[1] for row in conn.execute("PRAGMA table_info(subscriptions)").fetchall()]
    if "seats" not in sub_cols:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN seats INTEGER NOT NULL DEFAULT 1")


def write_snapshot(conn, subs, unresolved, synced_at):
    with conn:
        ensure_tables(conn)
        conn.execute("DELETE FROM subscriptions")
        conn.execute("DELETE FROM subscriptions_unresolved")
        for row in subs or []:
            conn.execute("""
                INSERT OR REPLACE INTO subscriptions
                    (email, tool, tier, monthly_fee_usd, seats, display_name, dept, synced_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                row.get("email") or "",
                row.get("tool") or "",
                row.get("tier") or "standard",
                float(row.get("monthly_fee_usd") or 0),
                int(row.get("seats") or 1),
                row.get("display_name") or "",
                row.get("dept") or "",
                synced_at,
            ))
        for row in unresolved or []:
            conn.execute("""
                INSERT OR REPLACE INTO subscriptions_unresolved
                    (tool, display_name, raw_email, dept, reason, synced_at)
                VALUES (?,?,?,?,?,?)
            """, (
                row.get("tool") or "",
                row.get("display_name") or "",
                row.get("raw_email") or "",
                row.get("dept") or "",
                row.get("reason") or "",
                synced_at,
            ))


def _json_request(url, payload=None, headers=None):
    body = None
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json; charset=utf-8")
    req = Request(url, data=body, headers=req_headers)
    resp = urlopen(req, timeout=TIMEOUT)
    raw = resp.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw or "{}")


def _get_tenant_access_token():
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        _die("missing FEISHU_APP_ID / FEISHU_APP_SECRET", 2)
    data = _json_request(
        FEISHU_HOST + "/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
    )
    if data.get("code") not in (None, 0):
        raise RuntimeError(data.get("msg") or "auth failed")
    token = data.get("tenant_access_token") or ""
    if not token:
        raise RuntimeError("auth returned empty tenant_access_token")
    return token


def _sheet_values(token, sheet_id):
    rng = quote(sheet_id + "!A1:Z5000", safe="")
    data = _json_request(
        FEISHU_HOST + "/open-apis/sheets/v2/spreadsheets/%s/values/%s" % (
            SPREADSHEET_TOKEN, rng),
        headers={"Authorization": "Bearer " + token},
    )
    if data.get("code") not in (None, 0):
        raise RuntimeError(data.get("msg") or ("fetch sheet %s failed" % sheet_id))
    value_range = data.get("data") or {}
    value_range = value_range.get("valueRange") or {}
    return value_range.get("values") or []


def _load_people(conn):
    rows = conn.execute("SELECT email, name, dept FROM people").fetchall()
    return [{"email": r[0] or "", "name": r[1] or "", "dept": r[2] or ""} for r in rows]


def _seat_rank(row):
    fee = float(row.get("monthly_fee_usd") or 0)
    tier = row.get("tier") or "standard"
    return (fee, 1 if tier == "premium" else 0)


def _aggregate_subscriptions(rows):
    grouped = {}
    for row in rows or []:
        email = row.get("email") or ""
        tool = row.get("tool") or ""
        if not email or not tool:
            continue
        key = (email, tool)
        item = grouped.get(key)
        if item is None:
            item = {
                "email": email,
                "tool": tool,
                "tier": row.get("tier") or "standard",
                "monthly_fee_usd": float(row.get("monthly_fee_usd") or 0),
                "seats": 1,
                "display_name": row.get("display_name") or "",
                "dept": row.get("dept") or "",
                "_best_rank": _seat_rank(row),
            }
            grouped[key] = item
            continue
        item["monthly_fee_usd"] = float(item.get("monthly_fee_usd") or 0) + float(row.get("monthly_fee_usd") or 0)
        item["seats"] = int(item.get("seats") or 0) + 1
        if not item.get("display_name") and row.get("display_name"):
            item["display_name"] = row.get("display_name") or ""
        if not item.get("dept") and row.get("dept"):
            item["dept"] = row.get("dept") or ""
        rank = _seat_rank(row)
        if rank > item.get("_best_rank"):
            item["tier"] = row.get("tier") or "standard"
            item["_best_rank"] = rank
    out = []
    for key in sorted(grouped):
        item = dict(grouped[key])
        item.pop("_best_rank", None)
        out.append(item)
    return out


def _build_snapshot(rows_by_tool, people):
    people_index = index_people(people)
    subs = []
    unresolved = []

    for row in rows_by_tool.get("codex") or []:
        resolved, miss = resolve_codex_identity(row, people_index)
        if resolved:
            subs.append({
                "email": resolved.get("email") or "",
                "tool": resolved.get("tool") or "",
                "tier": resolved.get("tier") or "standard",
                "monthly_fee_usd": float(resolved.get("monthly_fee_usd") or 0),
                "display_name": resolved.get("display_name") or "",
                "dept": resolved.get("dept") or "",
            })
        elif miss:
            unresolved.append(miss)

    for tool in ("claude", "cursor", "windsurf"):
        for row in rows_by_tool.get(tool) or []:
            subs.append({
                "email": row.get("raw_email") or "",
                "tool": row.get("tool") or "",
                "tier": row.get("tier") or "standard",
                "monthly_fee_usd": float(row.get("monthly_fee_usd") or 0),
                "display_name": row.get("display_name") or "",
                "dept": row.get("dept") or "",
            })
    return _aggregate_subscriptions(subs), unresolved


def _print_summary(rows_by_tool, subs, unresolved):
    for tool in ("codex", "claude", "cursor", "windsurf"):
        sys.stdout.write("%s: rows=%d\n" % (tool, len(rows_by_tool.get(tool) or [])))
    sys.stdout.write("resolved_subscriptions: %d\n" % len(subs))
    sys.stdout.write("unresolved: %d\n" % len(unresolved))


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    try:
        token = _get_tenant_access_token()
        rows_by_tool = {
            "codex": parse_codex_rows(_sheet_values(token, SHEETS["codex"])),
            "claude": parse_claude_rows(_sheet_values(token, SHEETS["claude"])),
            "cursor": parse_direct_email_rows(_sheet_values(token, SHEETS["cursor"]), "cursor", 40.0),
            "windsurf": parse_direct_email_rows(_sheet_values(token, SHEETS["windsurf"]), "windsurf", 30.0),
        }
    except (HTTPError, URLError) as e:
        _die("network failure: %s" % e, 1)
    except Exception as e:
        _die(str(e), 1)

    total_rows = sum(len(v) for v in rows_by_tool.values())
    if total_rows <= 0:
        _die("all subscription sheets are empty", 1)

    parent = os.path.dirname(os.path.abspath(DB))
    if parent:
        try:
            os.makedirs(parent)
        except OSError:
            pass
    conn = sqlite3.connect(DB)
    try:
        ensure_tables(conn)
        people = _load_people(conn)
        subs, unresolved = _build_snapshot(rows_by_tool, people)
        _print_summary(rows_by_tool, subs, unresolved)
        if dry_run:
            return 0
        write_snapshot(
            conn,
            subs,
            unresolved,
            datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # pragma: no cover
        _die(str(e), 1)
