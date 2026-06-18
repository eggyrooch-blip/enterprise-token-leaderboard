#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feishu contact directory sync — org source of truth for the token leaderboard.

Pulls departments + users via the Feishu contact v3 API using the bot's
``tenant_access_token`` and writes the directory tables
(``feishu_users`` / ``departments`` / ``department_attributions`` / ``roles``),
mirrors stable-email users into ``people``, and derives business-outsourcing
attribution back to real business departments.

Design goals (see ``docs/plans/2026-06-18-feishu-sso-org-auth.md`` Tasks 1-3):

* Pure stdlib so it can run on the production ``dev_collector`` host with no deps.
* ID-space discipline: Feishu joins (leader/owner/OAuth) are keyed on ``open_id``;
  local dashboard/auth keys are ``email``. The two never get conflated.
* Unit-testable with injected fake API responses — no live network in tests.
* Conservative attribution: only confident, active rows feed non-admin roll-ups.
  ``chat_owner_department`` is a *suggestion* (inactive) until a human promotes it,
  and anything we cannot resolve stays ``unresolved`` rather than being guessed.
* Safe nightly sync: a run that would downgrade a previously-active attribution
  refuses to apply the downgrade automatically and surfaces a review alert.
"""

import json
import os
import re
import sqlite3
import time
import unicodedata
from urllib.request import Request, urlopen

FEISHU_HOST = os.environ.get("FEISHU_HOST", "https://open.feishu.cn").rstrip("/")
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
TIMEOUT = int(os.environ.get("FEISHU_HTTP_TIMEOUT", "30"))

# Department children API rejects page_size=100 with a field-validation error;
# 50 is the proven working page size (see plan External API Facts).
DEPT_PAGE_SIZE = 50
USER_PAGE_SIZE = int(os.environ.get("FEISHU_USER_PAGE_SIZE", "50"))

# Spend buckets — preserve metric separation after roll-up.
BUCKET_EMPLOYEE = "employee_staff_outsourcing"   # 员工 + 人员外包
BUCKET_BUSINESS = "business_outsourcing"         # 业务外包 (supplier) attributed back
BUCKET_PENDING_BUSINESS = "pending_business_outsourcing"  # chat-owner suggestion
BUCKET_UNRESOLVED = "unresolved"

# Attribution rules / confidence vocab (must match the DB CHECK-free contract).
RULE_DIRECT = "direct_feishu_dept"
RULE_LEADER = "leader_department"
RULE_CHAT_OWNER = "chat_owner_department"
RULE_MANUAL = "manual_override"
RULE_UNRESOLVED = "unresolved"

CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_REVIEW = "needs_review"

# Active, non-admin-visible attributions come only from these rules by default.
ACTIVE_RULES = {RULE_DIRECT, RULE_LEADER, RULE_MANUAL}

MIN_RESOLVED_BUSINESS_OUTSOURCING_RATE = float(
    os.environ.get("MIN_RESOLVED_BUSINESS_OUTSOURCING_RATE", "0.8")
)

_SP_RE = re.compile(r"\(SP\d{4,}\)")


# --------------------------------------------------------------------------- #
# String / key helpers
# --------------------------------------------------------------------------- #
def _sstr(v):
    return "" if v is None else str(v)


def canonical_dept_key(raw_path):
    """Feishu/Feilian department path -> stable comparison key.

    Mirrors ``dev_collector._canonical_dept_key`` exactly so a Feilian raw path
    ``Keep/合作商/W/<supplier>`` and a Feishu path ``合作商/W/<supplier>`` collapse
    to the SAME key. NFKC, normalize slashes, collapse whitespace, drop a leading
    ``Keep/`` tenant root. Supplier codes like ``(SP000083)`` are preserved.
    """
    text = unicodedata.normalize("NFKC", _sstr(raw_path)).strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    parts = []
    for part in re.split(r"/+", text):
        part = re.sub(r"\s+", " ", part).strip()
        if part:
            parts.append(part)
    if parts and parts[0].lower() == "keep":
        parts = parts[1:]
    return "/".join(parts)


def is_outsourcing_department(path):
    """True for business-outsourcing supplier departments (under ``合作商/``).

    First-version detector: a ``合作商`` segment in the canonical path, or a
    supplier code ``(SP\\d+)`` in the raw string. Personnel outsourcing and
    regular employees are NOT outsourcing here — they fall through to the
    ``employee_staff_outsourcing`` bucket.
    """
    key = canonical_dept_key(path)
    parts = key.split("/") if key else []
    if "合作商" in parts:
        i = parts.index("合作商")
        if len(parts) > i + 1 and parts[i + 1] == "W":
            return True
    return bool(_SP_RE.search(_sstr(path)))


def _looks_like_supplier_entity(dept, path):
    """A real supplier node (vs a structural container like ``合作商`` / ``合作商/W``).

    Structural grouping nodes carry no spend and no supplier identity, so they
    are NOT resolved as suppliers — only actual company nodes (an ``(SP…)`` code,
    or a node with a responsible leader / department chat) are.
    """
    return bool(
        _SP_RE.search(_sstr(path))
        or _sstr(dept.get("leader_user_id"))
        or _sstr(dept.get("group_owner_user_id"))
        or _sstr(dept.get("chat_id"))
    )


def _now_iso(ts=None):
    if ts is None:
        ts = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# --------------------------------------------------------------------------- #
# Department tree / path building
# --------------------------------------------------------------------------- #
def build_department_paths(departments):
    """Return ``{dept_id: path}`` by walking parent links.

    Each department dict has ``dept_id``, ``parent_id`` (``"0"`` / "" for root)
    and ``name``. Cycles are broken defensively. The root tenant segment is left
    in place here; ``canonical_dept_key`` strips ``Keep/`` later.
    """
    by_id = {d["dept_id"]: d for d in departments if d.get("dept_id")}
    cache = {}

    def resolve(dept_id, seen):
        if dept_id in cache:
            return cache[dept_id]
        d = by_id.get(dept_id)
        if not d:
            return ""
        parent = _sstr(d.get("parent_id"))
        name = _sstr(d.get("name"))
        if not parent or parent in ("0", "od-0") or parent == dept_id or parent in seen:
            path = name
        else:
            seen.add(dept_id)
            prefix = resolve(parent, seen)
            path = (prefix + "/" + name) if prefix else name
        cache[dept_id] = path
        return path

    return {dept_id: resolve(dept_id, set()) for dept_id in by_id}


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def _existing_columns(conn, table):
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    except sqlite3.Error:
        return set()


def _add_column_if_missing(conn, table, column, ddl):
    if column not in _existing_columns(conn, table):
        conn.execute("ALTER TABLE %s ADD COLUMN %s" % (table, ddl))


def ensure_tables(conn):
    """Create/extend every table the directory sync owns. Idempotent."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS people(
            email TEXT PRIMARY KEY,
            name TEXT,
            avatar TEXT,
            dept TEXT)"""
    )
    for col, ddl in (
        ("feishu_user_id", "feishu_user_id TEXT"),
        ("feishu_open_id", "feishu_open_id TEXT"),
        ("status", "status TEXT DEFAULT 'active'"),
        ("source", "source TEXT DEFAULT ''"),
        ("raw_dept", "raw_dept TEXT DEFAULT ''"),
        ("effective_dept", "effective_dept TEXT DEFAULT ''"),
        ("attribution_source", "attribution_source TEXT DEFAULT ''"),
        ("spend_bucket", "spend_bucket TEXT DEFAULT '%s'" % BUCKET_EMPLOYEE),
        ("updated_at", "updated_at TEXT"),
    ):
        _add_column_if_missing(conn, "people", col, ddl)

    conn.execute(
        """CREATE TABLE IF NOT EXISTS feishu_users(
            open_id TEXT PRIMARY KEY,
            user_id TEXT DEFAULT '',
            union_id TEXT DEFAULT '',
            email TEXT DEFAULT '',
            name TEXT NOT NULL,
            dept_id TEXT DEFAULT '',
            dept_path TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            employee_type TEXT DEFAULT '',
            updated_at TEXT)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS departments(
            dept_id TEXT PRIMARY KEY,
            open_dept_id TEXT DEFAULT '',
            parent_id TEXT,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            leader_user_id TEXT DEFAULT '',
            chat_id TEXT DEFAULT '',
            group_owner_user_id TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            updated_at TEXT)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS department_attributions(
            source_dept_id TEXT PRIMARY KEY,
            source_dept_key TEXT NOT NULL,
            source_dept_path TEXT NOT NULL,
            target_dept_id TEXT DEFAULT '',
            target_dept_path TEXT DEFAULT '',
            spend_bucket TEXT NOT NULL,
            rule TEXT NOT NULL,
            confidence TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0,
            reason TEXT DEFAULT '',
            updated_at TEXT NOT NULL)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS roles(
            email TEXT NOT NULL,
            role TEXT NOT NULL,
            dept_id TEXT DEFAULT '',
            dept_path TEXT DEFAULT '',
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(email, role, dept_id))"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS role_overrides(
            email TEXT NOT NULL,
            role TEXT NOT NULL,
            dept_id TEXT DEFAULT '',
            action TEXT NOT NULL,
            reason TEXT DEFAULT '',
            PRIMARY KEY(email, role, dept_id, action))"""
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Attribution derivation (pure)
# --------------------------------------------------------------------------- #
def classify_spend_bucket(path, manual_overrides=None):
    """Bucket a department by its path (pre-resolution)."""
    if manual_overrides:
        ov = manual_overrides.get(canonical_dept_key(path))
        if ov and ov.get("spend_bucket"):
            return ov["spend_bucket"]
    return BUCKET_BUSINESS if is_outsourcing_department(path) else BUCKET_EMPLOYEE


def _primary_dept_path_for_open_id(open_id, users_by_open_id, dept_path_by_id):
    user = users_by_open_id.get(open_id)
    if not user:
        return None, None
    dept_id = _sstr(user.get("dept_id"))
    path = user.get("dept_path") or dept_path_by_id.get(dept_id, "")
    if not path:
        return None, None
    return dept_id, path


def derive_department_attributions(
    departments,
    users,
    chat_owner_lookup=None,
    manual_overrides=None,
):
    """Derive one attribution row per department. Pure — no DB, no network.

    Rules in order (plan Task 3):
      1. Manual override wins.
      2. Non-outsourcing path -> itself, ``direct_feishu_dept`` / employee bucket.
      3. (Personnel outsourcing collapses into rule 2 for v1.)
      4. Business outsourcing + ``leader_user_id`` -> leader's real dept,
         ``leader_department`` / business bucket / high / active.
      5. Business outsourcing + readable ``chat_id`` owner -> owner's real dept,
         ``chat_owner_department`` / business bucket / medium / INACTIVE until promoted.
      6. If the resolved target is itself outsourcing -> unresolved (no cycles).
      7. Else unresolved; preserve source path.

    Key-conflict guard: if two departments normalize to the same
    ``source_dept_key``, BOTH are written inactive/unresolved with
    ``reason=key_conflict``.
    """
    manual_overrides = manual_overrides or {}
    dept_path_by_id = build_department_paths(departments)
    users_by_open_id = {u["open_id"]: u for u in users if u.get("open_id")}

    rows = []
    for d in departments:
        dept_id = _sstr(d.get("dept_id"))
        if not dept_id:
            continue
        path = d.get("path") or dept_path_by_id.get(dept_id, "") or _sstr(d.get("name"))
        key = canonical_dept_key(path)
        base = {
            "source_dept_id": dept_id,
            "source_dept_key": key,
            "source_dept_path": path,
            "target_dept_id": "",
            "target_dept_path": "",
        }

        # 1. Manual override.
        ov = manual_overrides.get(key)
        if ov:
            rows.append(dict(base,
                             target_dept_path=ov.get("target_dept_path", path),
                             target_dept_id=ov.get("target_dept_id", ""),
                             spend_bucket=ov.get("spend_bucket", BUCKET_BUSINESS),
                             rule=RULE_MANUAL, confidence=CONF_HIGH,
                             active=1, reason="manual_override"))
            continue

        # 2/3. Non-outsourcing -> direct.
        if not is_outsourcing_department(path):
            rows.append(dict(base,
                             target_dept_id=dept_id, target_dept_path=path,
                             spend_bucket=BUCKET_EMPLOYEE, rule=RULE_DIRECT,
                             confidence=CONF_HIGH, active=1, reason=""))
            continue

        # Structural container nodes under the outsourcing tree (合作商, 合作商/W)
        # carry no spend and no supplier identity — treat as direct/structural.
        if not _looks_like_supplier_entity(d, path):
            rows.append(dict(base, target_dept_id=dept_id, target_dept_path=path,
                             spend_bucket=BUCKET_EMPLOYEE, rule=RULE_DIRECT,
                             confidence=CONF_HIGH, active=1, reason="outsourcing_container"))
            continue

        # Business outsourcing supplier department -> try to resolve a real dept.
        leader = _sstr(d.get("leader_user_id"))
        resolved = None
        if leader:
            tdid, tpath = _primary_dept_path_for_open_id(
                leader, users_by_open_id, dept_path_by_id)
            if tpath:
                resolved = (tdid, tpath, RULE_LEADER, CONF_HIGH, 1)

        if resolved is None:
            owner_open_id = _sstr(d.get("group_owner_user_id"))
            chat_id = _sstr(d.get("chat_id"))
            if chat_id and chat_owner_lookup:
                owner_open_id = owner_open_id or _sstr(chat_owner_lookup(chat_id))
            if owner_open_id:
                tdid, tpath = _primary_dept_path_for_open_id(
                    owner_open_id, users_by_open_id, dept_path_by_id)
                if tpath:
                    # medium confidence, INACTIVE until a human promotes it.
                    resolved = (tdid, tpath, RULE_CHAT_OWNER, CONF_MEDIUM, 0)

        if resolved is None:
            rows.append(dict(base, spend_bucket=BUCKET_UNRESOLVED,
                             rule=RULE_UNRESOLVED, confidence=CONF_REVIEW,
                             active=0, reason="no_resolvable_owner",
                             target_dept_path=path))
            continue

        tdid, tpath, rule, conf, active = resolved
        bucket = BUCKET_PENDING_BUSINESS if rule == RULE_CHAT_OWNER and not active else BUCKET_BUSINESS
        # 6. No supplier-to-supplier cycles.
        if is_outsourcing_department(tpath):
            rows.append(dict(base, spend_bucket=BUCKET_UNRESOLVED,
                             rule=RULE_UNRESOLVED, confidence=CONF_REVIEW,
                             active=0, reason="resolved_target_is_outsourcing",
                             target_dept_path=path))
            continue

        rows.append(dict(base, target_dept_id=tdid, target_dept_path=tpath,
                         spend_bucket=bucket, rule=rule,
                         confidence=conf, active=active, reason=""))

    # Key-conflict guard: same canonical key on >1 department -> all inactive.
    by_key = {}
    for r in rows:
        by_key.setdefault(r["source_dept_key"], []).append(r)
    for key, group in by_key.items():
        if key and len(group) > 1:
            for r in group:
                r.update(spend_bucket=BUCKET_UNRESOLVED, rule=RULE_UNRESOLVED,
                         confidence=CONF_REVIEW, active=0, reason="key_conflict")
    return rows


def effective_dept_for_person(raw_dept_path, attributions):
    """Resolve a raw department path to its effective department.

    Returns ``(effective_path, spend_bucket, attribution_source)``. Only resolves
    when EXACTLY ONE active attribution exists for the canonical key — zero or
    multiple active rows fall back to the raw path with an ``unresolved`` source.
    """
    key = canonical_dept_key(raw_dept_path)
    if not key:
        return raw_dept_path, BUCKET_EMPLOYEE, ""
    active = [a for a in attributions
              if a.get("source_dept_key") == key and a.get("active")]
    if len(active) == 1:
        a = active[0]
        return (a.get("target_dept_path") or raw_dept_path,
                a.get("spend_bucket") or BUCKET_EMPLOYEE,
                a.get("rule") or "")
    pending = [
        a for a in attributions
        if a.get("source_dept_key") == key
        and not a.get("active")
        and (
            a.get("rule") == RULE_CHAT_OWNER
            or a.get("spend_bucket") == BUCKET_PENDING_BUSINESS
        )
        and a.get("target_dept_path")
    ]
    if len(pending) == 1:
        a = pending[0]
        return (a.get("target_dept_path") or raw_dept_path,
                BUCKET_PENDING_BUSINESS,
                a.get("rule") or "")
    # No confident/candidate attribution: preserve raw path, signal review if it's a supplier.
    if is_outsourcing_department(raw_dept_path):
        return raw_dept_path, BUCKET_UNRESOLVED, RULE_UNRESOLVED
    return raw_dept_path, BUCKET_EMPLOYEE, RULE_DIRECT


def disable_unapproved_business_rollup(attributions):
    """Keep supplier candidates visible without enabling production roll-up."""
    rows = []
    for attr in attributions:
        row = dict(attr)
        if (
            is_outsourcing_department(row.get("source_dept_path", ""))
            and row.get("active")
            and row.get("spend_bucket") == BUCKET_BUSINESS
            and row.get("rule") != RULE_MANUAL
        ):
            row["active"] = 0
            row["spend_bucket"] = BUCKET_PENDING_BUSINESS
            row["reason"] = "production_enablement_blocked_low_coverage"
        rows.append(row)
    return rows


def resolved_business_outsourcing_rate(attributions, spend_by_key=None):
    """active resolved supplier spend / total supplier spend.

    ``spend_by_key`` maps canonical key -> spend weight; when omitted each
    supplier department counts as 1 (row-count proxy used in dry-run preview).
    """
    total = 0.0
    resolved = 0.0
    for a in attributions:
        path = a.get("source_dept_path", "")
        if not is_outsourcing_department(path):
            continue
        if a.get("spend_bucket") == BUCKET_EMPLOYEE:
            continue  # structural container node, no supplier spend
        w = 1.0 if spend_by_key is None else float(
            spend_by_key.get(a.get("source_dept_key"), 0.0))
        total += w
        if a.get("active") and a.get("rule") in ACTIVE_RULES:
            resolved += w
    if total <= 0:
        return 1.0
    return resolved / total


# --------------------------------------------------------------------------- #
# Snapshot writer
# --------------------------------------------------------------------------- #
def write_directory_snapshot(
    conn,
    users,
    departments,
    admin_emails=None,
    synced_at=None,
    chat_owner_lookup=None,
    manual_overrides=None,
    allow_partial=False,
    business_rollup_enabled=True,
):
    """Persist a full directory snapshot. Idempotent. Returns a summary dict.

    * ``feishu_users`` keyed by ``open_id`` (supplier rows with empty email kept).
    * ``people`` mirrored only for stable-email users.
    * ``departments`` with raw path + ``chat_id`` / ``leader_user_id`` / open id.
    * ``department_attributions`` derived via :func:`derive_department_attributions`,
      with downgrade protection: a previously-active attribution is never silently
      flipped inactive — the old active row is preserved and an alert is recorded.
    * ``roles``: ``department_owner`` from each dept ``leader_user_id`` (joined on
      ``open_id``), ``admin`` from ``admin_emails``; ``role_overrides`` deny wins.
    """
    ensure_tables(conn)
    admin_emails = {e.strip().lower() for e in (admin_emails or []) if e and e.strip()}
    stamp = _now_iso(synced_at if isinstance(synced_at, (int, float)) else None)
    if isinstance(synced_at, str) and synced_at:
        stamp = synced_at

    dept_path_by_id = build_department_paths(departments)
    users_by_open_id = {u["open_id"]: u for u in users if u.get("open_id")}

    alerts = []

    # --- feishu_users + people mirror ---
    for u in users:
        open_id = _sstr(u.get("open_id"))
        if not open_id:
            continue
        dept_id = _sstr(u.get("dept_id"))
        dept_path = u.get("dept_path") or dept_path_by_id.get(dept_id, "")
        email = _sstr(u.get("email")).strip().lower()
        status = _sstr(u.get("status") or "active")
        conn.execute(
            """INSERT INTO feishu_users(open_id,user_id,union_id,email,name,
                   dept_id,dept_path,status,employee_type,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(open_id) DO UPDATE SET
                   user_id=excluded.user_id, union_id=excluded.union_id,
                   email=excluded.email, name=excluded.name, dept_id=excluded.dept_id,
                   dept_path=excluded.dept_path, status=excluded.status,
                   employee_type=excluded.employee_type, updated_at=excluded.updated_at""",
            (open_id, _sstr(u.get("user_id")), _sstr(u.get("union_id")), email,
             _sstr(u.get("name")), dept_id, dept_path, status,
             _sstr(u.get("employee_type")), stamp),
        )

    # --- departments ---
    for d in departments:
        dept_id = _sstr(d.get("dept_id"))
        if not dept_id:
            continue
        path = d.get("path") or dept_path_by_id.get(dept_id, "") or _sstr(d.get("name"))
        conn.execute(
            """INSERT INTO departments(dept_id,open_dept_id,parent_id,name,path,
                   leader_user_id,chat_id,group_owner_user_id,status,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(dept_id) DO UPDATE SET
                   open_dept_id=excluded.open_dept_id, parent_id=excluded.parent_id,
                   name=excluded.name, path=excluded.path,
                   leader_user_id=excluded.leader_user_id, chat_id=excluded.chat_id,
                   group_owner_user_id=excluded.group_owner_user_id,
                   status=excluded.status, updated_at=excluded.updated_at""",
            (dept_id, _sstr(d.get("open_dept_id")), _sstr(d.get("parent_id")),
             _sstr(d.get("name")), path, _sstr(d.get("leader_user_id")),
             _sstr(d.get("chat_id")), _sstr(d.get("group_owner_user_id")),
             _sstr(d.get("status") or "active"), stamp),
        )

    # --- department_attributions (with downgrade protection) ---
    new_rows = derive_department_attributions(
        departments, users, chat_owner_lookup, manual_overrides)
    if not business_rollup_enabled:
        new_rows = disable_unapproved_business_rollup(new_rows)
    for r in new_rows:
        sid = r["source_dept_id"]
        prev = conn.execute(
            "SELECT active, target_dept_path, rule, confidence, spend_bucket"
            " FROM department_attributions WHERE source_dept_id=?", (sid,)
        ).fetchone()
        active = int(r["active"])
        rule, confidence, bucket = r["rule"], r["confidence"], r["spend_bucket"]
        target_path = r["target_dept_path"]
        target_id = r["target_dept_id"]
        reason = r["reason"]
        if (prev and int(prev[0]) == 1 and active == 0
                and rule != RULE_MANUAL
                and "production_enablement_blocked_low_coverage" not in reason):
            # Refuse to auto-downgrade a previously-active attribution.
            alerts.append({
                "source_dept_id": sid, "kind": "downgrade_blocked",
                "previous_target": prev[1], "would_become": reason or rule,
            })
            active = 1
            target_path = prev[1] or target_path
            rule = prev[2] or rule
            confidence = prev[3] or confidence
            bucket = prev[4] or bucket
            reason = (reason + ";downgrade_blocked").strip(";")
        conn.execute(
            """INSERT INTO department_attributions(source_dept_id,source_dept_key,
                   source_dept_path,target_dept_id,target_dept_path,spend_bucket,
                   rule,confidence,active,reason,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_dept_id) DO UPDATE SET
                   source_dept_key=excluded.source_dept_key,
                   source_dept_path=excluded.source_dept_path,
                   target_dept_id=excluded.target_dept_id,
                   target_dept_path=excluded.target_dept_path,
                   spend_bucket=excluded.spend_bucket, rule=excluded.rule,
                   confidence=excluded.confidence, active=excluded.active,
                   reason=excluded.reason, updated_at=excluded.updated_at""",
            (sid, r["source_dept_key"], r["source_dept_path"], target_id,
             target_path, bucket, rule, confidence, active, reason, stamp),
        )

    # --- roles: department_owner from leaders, admin from admin_emails ---
    overrides = {
        (e.lower(), role, did): action
        for (e, role, did, action) in conn.execute(
            "SELECT email, role, COALESCE(dept_id,''), action FROM role_overrides"
        ).fetchall()
    }

    def _denied(email, role, dept_id=""):
        for (oe, orole, odid), action in overrides.items():
            if action == "deny" and oe == email and orole == role and odid in ("", dept_id):
                return True
        return False

    conn.execute("DELETE FROM roles WHERE source='feishu_sync'")
    written_roles = 0
    for d in departments:
        leader = _sstr(d.get("leader_user_id"))
        if not leader:
            continue
        lu = users_by_open_id.get(leader)
        if not lu or not _sstr(lu.get("email")).strip():
            if not allow_partial:
                raise ValueError(
                    "department %s leader %s not joinable to a snapshot open_id "
                    "with email (pass allow_partial=True to tolerate)"
                    % (d.get("dept_id"), leader))
            alerts.append({"source_dept_id": _sstr(d.get("dept_id")),
                           "kind": "leader_unjoinable", "leader_open_id": leader})
            continue
        email = _sstr(lu.get("email")).strip().lower()
        dept_id = _sstr(d.get("dept_id"))
        path = d.get("path") or dept_path_by_id.get(dept_id, "")
        if _denied(email, "department_owner", dept_id):
            continue
        conn.execute(
            """INSERT INTO roles(email,role,dept_id,dept_path,source,updated_at)
               VALUES(?,?,?,?, 'feishu_sync', ?)
               ON CONFLICT(email,role,dept_id) DO UPDATE SET
                   dept_path=excluded.dept_path, source='feishu_sync',
                   updated_at=excluded.updated_at""",
            (email, "department_owner", dept_id, path, stamp),
        )
        written_roles += 1

    for email in sorted(admin_emails):
        if _denied(email, "admin"):
            continue
        conn.execute(
            """INSERT INTO roles(email,role,dept_id,dept_path,source,updated_at)
               VALUES(?, 'admin', '', '', 'feishu_sync', ?)
               ON CONFLICT(email,role,dept_id) DO UPDATE SET
                   source='feishu_sync', updated_at=excluded.updated_at""",
            (email, stamp),
        )
        written_roles += 1

    # --- people mirror (stable-email users only) ---
    for u in users:
        email = _sstr(u.get("email")).strip().lower()
        if not email:
            continue  # supplier / emailless users live only in feishu_users
        dept_id = _sstr(u.get("dept_id"))
        raw_path = u.get("dept_path") or dept_path_by_id.get(dept_id, "")
        eff_path, bucket, attr_src = effective_dept_for_person(raw_path, new_rows)
        conn.execute(
            """INSERT INTO people(email,name,dept,feishu_user_id,feishu_open_id,
                   status,source,raw_dept,effective_dept,attribution_source,
                   spend_bucket,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(email) DO UPDATE SET
                   name=excluded.name, dept=excluded.dept,
                   feishu_user_id=excluded.feishu_user_id,
                   feishu_open_id=excluded.feishu_open_id, status=excluded.status,
                   source='feishu', raw_dept=excluded.raw_dept,
                   effective_dept=excluded.effective_dept,
                   attribution_source=excluded.attribution_source,
                   spend_bucket=excluded.spend_bucket, updated_at=excluded.updated_at""",
            (email, _sstr(u.get("name")), eff_path, _sstr(u.get("user_id")),
             _sstr(u.get("open_id")), _sstr(u.get("status") or "active"), "feishu",
             raw_path, eff_path, attr_src, bucket, stamp),
        )

    conn.commit()

    supplier_rows = [r for r in new_rows if is_outsourcing_department(r["source_dept_path"])]
    return {
        "users": len(users_by_open_id),
        "departments": sum(1 for d in departments if d.get("dept_id")),
        "attributions": len(new_rows),
        "supplier_departments": len(supplier_rows),
        "unresolved": sum(1 for r in new_rows if r["rule"] == RULE_UNRESOLVED),
        "roles_written": written_roles,
        "resolved_business_outsourcing_rate": resolved_business_outsourcing_rate(new_rows),
        "business_rollup_enabled": bool(business_rollup_enabled),
        "alerts": alerts,
        "synced_at": stamp,
    }


# --------------------------------------------------------------------------- #
# Feishu contact API client
# --------------------------------------------------------------------------- #
def _default_json_request(url, payload=None, headers=None, method=None):
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json; charset=utf-8")
    req = Request(url, data=body, headers=req_headers, method=method)
    resp = urlopen(req, timeout=TIMEOUT)
    raw = resp.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw or "{}")


class FeishuDirectoryClient(object):
    """Reads the org directory via Feishu contact v3 using ``tenant_access_token``.

    ``json_request`` is injectable so unit tests feed fake API responses without
    hitting the network. Joins are pinned to ``open_id``; pagination uses the
    proven page sizes from the plan (departments page_size=50, no ``fetch_child``
    on the children endpoint).
    """

    def __init__(self, app_id=None, app_secret=None, host=None, json_request=None):
        self.app_id = (app_id or FEISHU_APP_ID).strip()
        self.app_secret = (app_secret or FEISHU_APP_SECRET).strip()
        self.host = (host or FEISHU_HOST).rstrip("/")
        self._json_request = json_request or _default_json_request
        self._token = None

    # -- auth --
    def tenant_access_token(self):
        if self._token:
            return self._token
        if not self.app_id or not self.app_secret:
            raise RuntimeError("missing FEISHU_APP_ID / FEISHU_APP_SECRET")
        data = self._json_request(
            self.host + "/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self.app_id, "app_secret": self.app_secret})
        if data.get("code") not in (None, 0):
            raise RuntimeError(data.get("msg") or "auth failed")
        token = data.get("tenant_access_token") or ""
        if not token:
            raise RuntimeError("auth returned empty tenant_access_token")
        self._token = token
        return token

    def _auth_headers(self):
        return {"Authorization": "Bearer " + self.tenant_access_token()}

    def _get(self, path, params):
        from urllib.parse import urlencode
        url = self.host + path + "?" + urlencode(params)
        data = self._json_request(url, None, self._auth_headers())
        if data.get("code") not in (None, 0):
            raise RuntimeError(data.get("msg") or ("GET %s failed" % path))
        return data.get("data") or {}

    # -- departments --
    def list_department_children(self, dept_id):
        items, page_token = [], None
        while True:
            params = {
                "department_id_type": "department_id",
                "user_id_type": "open_id",
                "page_size": DEPT_PAGE_SIZE,
            }
            if page_token:
                params["page_token"] = page_token
            data = self._get(
                "/open-apis/contact/v3/departments/%s/children" % dept_id, params)
            items.extend(data.get("items") or [])
            if data.get("has_more") and data.get("page_token"):
                page_token = data["page_token"]
            else:
                break
        return items

    def list_departments(self, root="0"):
        """Recursively traverse the department tree from ``root``."""
        seen, out, stack = set(), [], [root]
        while stack:
            current = stack.pop()
            for item in self.list_department_children(current):
                did = _sstr(item.get("department_id") or item.get("open_department_id"))
                if not did or did in seen:
                    continue
                seen.add(did)
                out.append(self._normalize_department(item))
                stack.append(did)
        return out

    def get_department(self, dept_id):
        data = self._get(
            "/open-apis/contact/v3/departments/%s" % dept_id,
            {"department_id_type": "department_id", "user_id_type": "open_id"})
        return self._normalize_department(data.get("department") or data)

    @staticmethod
    def _normalize_department(item):
        leaders = item.get("leaders") or []
        group_owner = ""
        for ldr in leaders:
            if ldr.get("leaderType") == 2 or ldr.get("leader_type") == 2:
                group_owner = _sstr(ldr.get("leaderID") or ldr.get("leader_id"))
        return {
            "dept_id": _sstr(item.get("department_id")),
            "open_dept_id": _sstr(item.get("open_department_id")),
            "parent_id": _sstr(item.get("parent_department_id")),
            "name": _sstr(item.get("name")),
            "leader_user_id": _sstr(item.get("leader_user_id")),
            "chat_id": _sstr(item.get("chat_id")),
            "group_owner_user_id": group_owner,
            "member_count": item.get("member_count"),
            "primary_member_count": item.get("primary_member_count"),
            "status": "inactive" if (item.get("status") or {}).get("is_deleted") else "active",
        }

    # -- users --
    def list_users_by_department(self, dept_id, fetch_child=True):
        items, page_token = [], None
        while True:
            params = {
                "department_id": dept_id,
                "department_id_type": "department_id",
                "user_id_type": "open_id",
                "page_size": USER_PAGE_SIZE,
                "fetch_child": "true" if fetch_child else "false",
            }
            if page_token:
                params["page_token"] = page_token
            data = self._get("/open-apis/contact/v3/users/find_by_department", params)
            items.extend(data.get("items") or [])
            if data.get("has_more") and data.get("page_token"):
                page_token = data["page_token"]
            else:
                break
        return [self._normalize_user(u) for u in items]

    @staticmethod
    def _normalize_user(u):
        status = u.get("status") or {}
        is_active = not (status.get("is_resigned") or status.get("is_frozen")
                         or status.get("is_unjoin"))
        dept_ids = u.get("department_ids") or []
        return {
            "open_id": _sstr(u.get("open_id")),
            "user_id": _sstr(u.get("user_id")),
            "union_id": _sstr(u.get("union_id")),
            "email": _sstr(u.get("email") or u.get("enterprise_email")),
            "name": _sstr(u.get("name")),
            "dept_id": _sstr(dept_ids[0]) if dept_ids else "",
            "employee_type": _sstr(u.get("employee_type")),
            "status": "active" if is_active else "inactive",
        }

    def fetch_snapshot(self, root="0"):
        departments = self.list_departments(root)
        path_by_id = build_department_paths(departments)
        for d in departments:
            d["path"] = path_by_id.get(d["dept_id"], d.get("name", ""))
        users, seen = [], set()
        # find_by_department from root with fetch_child=true returns the whole tree.
        for u in self.list_users_by_department(root, fetch_child=True):
            if u["open_id"] and u["open_id"] not in seen:
                seen.add(u["open_id"])
                u["dept_path"] = path_by_id.get(u["dept_id"], "")
                users.append(u)
        return departments, users

    def validate_visibility_coverage(self, departments, users):
        """Compare fetched user counts to department member_count; warn on gaps."""
        warnings = []
        counts = {}
        for u in users:
            counts[u["dept_id"]] = counts.get(u["dept_id"], 0) + 1
        for d in departments:
            expected = d.get("primary_member_count") or d.get("member_count")
            if expected is None:
                continue
            got = counts.get(d["dept_id"], 0)
            if got < int(expected):
                warnings.append({
                    "dept_id": d["dept_id"], "path": d.get("path", ""),
                    "expected": int(expected), "got": got,
                })
        return warnings


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Feishu directory sync")
    parser.add_argument("--db", default=os.environ.get("DEV_DB", "dev.db"))
    parser.add_argument("--root", default=os.environ.get("FEISHU_ROOT_DEPT", "0"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--admin-emails", default=os.environ.get("AUTH_ADMIN_EMAILS", ""))
    parser.add_argument(
        "--allow-low-coverage",
        action="store_true",
        default=os.environ.get("ALLOW_LOW_FEISHU_ATTRIBUTION_COVERAGE", "").strip() == "1",
        help="write even when resolved business-outsourcing coverage is below threshold",
    )
    args = parser.parse_args(argv)

    client = FeishuDirectoryClient()
    departments, users = client.fetch_snapshot(args.root)
    warnings = client.validate_visibility_coverage(departments, users)
    attributions = derive_department_attributions(departments, users)
    rate = resolved_business_outsourcing_rate(attributions)
    admin_emails = [e for e in re.split(r"[,\s]+", args.admin_emails) if e]

    summary = {
        "departments": len(departments),
        "users": len(users),
        "supplier_departments": sum(
            1 for a in attributions if is_outsourcing_department(a["source_dept_path"])),
        "unresolved": sum(1 for a in attributions if a["rule"] == RULE_UNRESOLVED),
        "resolved_business_outsourcing_rate": round(rate, 4),
        "visibility_warnings": warnings,
        "min_required_rate": MIN_RESOLVED_BUSINESS_OUTSOURCING_RATE,
        "production_enablement_blocked": rate < MIN_RESOLVED_BUSINESS_OUTSOURCING_RATE,
    }

    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    business_rollup_enabled = (
        not summary["production_enablement_blocked"] or args.allow_low_coverage
    )
    conn = sqlite3.connect(args.db)
    try:
        result = write_directory_snapshot(
            conn, users, departments, admin_emails=admin_emails,
            business_rollup_enabled=business_rollup_enabled)
    finally:
        conn.close()
    result.update({
        "visibility_warnings": warnings,
        "min_required_rate": MIN_RESOLVED_BUSINESS_OUTSOURCING_RATE,
        "production_enablement_blocked": summary["production_enablement_blocked"],
        "business_rollup_enabled": business_rollup_enabled,
    })
    if summary["production_enablement_blocked"] and not args.allow_low_coverage:
        result["override"] = "--allow-low-coverage"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
