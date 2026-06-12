#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步 Feishu 表格里的订阅名单到本地 SQLite。

目标环境：CentOS7 / Python 3.6.8 / SQLite 3.7.17 / 纯标准库。
"""
from __future__ import print_function

import datetime
import json
import os
import re
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


def _today_str():
    return datetime.date.today().strftime("%Y-%m-%d")


_DEL_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


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


def _is_deleted(raw):
    if isinstance(raw, bool):
        return raw
    s = str(raw or "").strip().lower()
    return s in ("true", "1", "yes", "y", "是")


def _extract_del_date(remark_text):
    m = _DEL_DATE_RE.search(remark_text or "")
    return m.group(1) if m else None


def _codex_start(cell):
    s = (cell or "").strip()
    return s if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else None


def _claude_start(cell):
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", (cell or "").strip())
    return ("%s-%s-%s" % (m.group(1), m.group(2), m.group(3))) if m else None


def _end_date_for(deleted, remark_text):
    if not deleted:
        return None
    return _extract_del_date(remark_text) or _today_str()


def parse_codex_rows(rows):
    out = []
    for i, row in enumerate(rows or []):
        if i == 0:
            continue
        raw_email = _cell(row, 2)
        if not raw_email:
            continue
        raw_q = row[16] if 16 < len(row) else ""
        deleted = _is_deleted(raw_q)
        start_date = _codex_start(_cell(row, 4))
        remark = _cell(row, 15)
        out.append({
            "tool": "codex",
            "display_name": _cell(row, 5),
            "raw_email": raw_email,
            "dept": _cell(row, 6),
            "tier": "standard",
            "monthly_fee_usd": 25.0,
            "start_date": start_date,
            "end_date": _end_date_for(deleted, remark),
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
        raw_k = row[10] if 10 < len(row) else ""
        deleted = _is_deleted(raw_k)
        start_date = _claude_start(_cell(row, 9))
        out.append({
            "tool": "claude",
            "display_name": _cell(row, 5),
            "raw_email": user_id + "@keep.com",
            "dept": _cell(row, 6),
            "tier": "premium" if premium else "standard",
            "monthly_fee_usd": 100.0 if premium else 25.0,
            "start_date": start_date,
            "end_date": _end_date_for(deleted, remark),
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
            "start_date": None,
            "end_date": None,
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
    # 旧库是「同人同工具聚合成一行 + seats 计数」的模型；新模型改为「一席一行」，
    # 主键 (email, tool, seat)。检测到老表(无 seat 列)直接 DROP 重建 —— 本表每天整表覆盖，
    # 丢弃旧行安全(次日同步即重灌)，不做逐列 ALTER 迁移。
    existing = [row[1] for row in conn.execute("PRAGMA table_info(subscriptions)").fetchall()]
    if existing and "seat" not in existing:
        conn.execute("DROP TABLE subscriptions")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions(
            email TEXT NOT NULL,
            tool TEXT NOT NULL,
            seat INTEGER NOT NULL DEFAULT 1,
            tier TEXT NOT NULL DEFAULT 'standard',
            monthly_fee_usd REAL NOT NULL DEFAULT 0,
            display_name TEXT DEFAULT '',
            dept TEXT DEFAULT '',
            start_date TEXT,
            end_date TEXT,
            synced_at TEXT NOT NULL,
            PRIMARY KEY(email, tool, seat)
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


def write_snapshot(conn, subs, unresolved, synced_at):
    with conn:
        ensure_tables(conn)
        conn.execute("DELETE FROM subscriptions")
        conn.execute("DELETE FROM subscriptions_unresolved")
        for row in subs or []:
            conn.execute("""
                INSERT OR REPLACE INTO subscriptions
                    (email, tool, seat, tier, monthly_fee_usd, display_name, dept, start_date, end_date, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                row.get("email") or "",
                row.get("tool") or "",
                int(row.get("seat") or 1),
                row.get("tier") or "standard",
                float(row.get("monthly_fee_usd") or 0),
                row.get("display_name") or "",
                row.get("dept") or "",
                row.get("start_date"),
                row.get("end_date"),
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


def _seat_row(email, src, seat):
    """单席行：保留各账号自己的 tier/月费/生命周期区间，不与同人同工具的其他席位合并。"""
    return {
        "email": email,
        "tool": src.get("tool") or "",
        "seat": seat,
        "tier": src.get("tier") or "standard",
        "monthly_fee_usd": float(src.get("monthly_fee_usd") or 0),
        "display_name": src.get("display_name") or "",
        "dept": src.get("dept") or "",
        "start_date": src.get("start_date"),
        "end_date": src.get("end_date"),
    }


def _build_snapshot(rows_by_tool, people):
    """一席一行：每个名单条目就是一行，seat 在 (email,tool) 内按输入(表单)顺序 1..N 枚举。
    同人同工具的多账号不再合并 —— 一个在用席位 + 一个已删除席位各自按自己的区间计费/挂徽章，
    杜绝「两份月费、无终止日」的旧聚合超收。"""
    people_index = index_people(people)
    subs = []
    unresolved = []
    seat_counter = {}   # (email, tool) -> 已分配席位数,保证 seat 在该键内递增且确定

    def _emit(email, src):
        tool = src.get("tool") or ""
        key = (email, tool)
        seat = seat_counter.get(key, 0) + 1
        seat_counter[key] = seat
        subs.append(_seat_row(email, src, seat))

    for row in rows_by_tool.get("codex") or []:
        resolved, miss = resolve_codex_identity(row, people_index)
        if resolved:
            _emit(resolved.get("email") or "", resolved)
        elif miss:
            unresolved.append(miss)

    for tool in ("claude", "cursor", "windsurf"):
        for row in rows_by_tool.get(tool) or []:
            _emit(row.get("raw_email") or "", row)
    return subs, unresolved


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
