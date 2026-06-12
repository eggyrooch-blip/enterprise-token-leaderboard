# Subscription Cost Badges Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add subscription roster sync, subscription-backed company-paid cost semantics, person badges, and governance visibility to the token leaderboard in both SQLite and Postgres paths.

**Architecture:** Keep the existing one-file collector pattern. Add `collector/subscriptions_sync.py` as a stdlib-only Feishu roster sync job that full-replaces `subscriptions` and `subscriptions_unresolved` in SQLite. Extend `collector/dev_collector.py` and `collector/app.py` to compute person-board `公司实付` as gateway actuals plus current-subscription monthly fees multiplied by overlapped calendar months, and to expose per-person `subs` plus unresolved governance counts. Reuse the current “merge extra people into person board” pattern so pure-subscription people can appear without usage rows.

**Tech Stack:** Python 3.6 stdlib + sqlite3, FastAPI/asyncpg Postgres parity, vanilla JS dashboard, pytest.

### Task 1: Contract Tests For Parsing And Cost Semantics

**Files:**
- Create: `tests/test_subscriptions_sync.py`
- Modify: `tests/test_dev_collector_db.py`
- Modify: `tests/test_governance_metrics_api.py`

**Step 1: Write failing tests**

Add tests that assert:
- sheet-row parsing skips headers and empty identity rows, applies the exact column maps, and detects Claude premium via the remark column;
- Codex identity resolution uses direct `@keep.com`, unique people-name matches, unresolved `ambiguous`, and unresolved `no_match`;
- `months_overlapped()` returns correct calendar-month counts;
- SQLite subscription snapshot replacement is idempotent and removed people disappear;
- person leaderboard cost excludes `source='subscription'` list-price cost, includes `source='litellm'` actual cost, includes subscription fees, carries `subs`, surfaces pure-subscription people, and removes fee/badge after resync deletion;
- governance payload exposes subscription unresolved count.

**Step 2: Run focused tests to verify they fail**

Run:

```bash
python3 -m pytest tests/test_subscriptions_sync.py tests/test_dev_collector_db.py tests/test_governance_metrics_api.py -q
```

Expected: FAIL because subscription sync helpers, schema, and cost semantics do not exist yet.

### Task 2: SQLite Sync Job And Collector Logic

**Files:**
- Create: `collector/subscriptions_sync.py`
- Modify: `collector/dev_collector.py`

**Step 1: Implement minimal backend**

Implement:
- new SQLite tables in `db()`;
- stdlib Feishu auth + sheets fetch + parsing + identity resolution + one-transaction full-replace write;
- pure helpers for parsing rows, resolving identities, `months_overlapped()`, window derivation, current-subscription loading, and person cost calculation;
- person-board merge of pure-subscription people and `subs` emission;
- governance unresolved count from `subscriptions_unresolved`.

**Step 2: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_subscriptions_sync.py tests/test_dev_collector_db.py tests/test_governance_metrics_api.py -q
```

Expected: PASS.

### Task 3: Postgres Parity And UI

**Files:**
- Modify: `collector/schema.sql`
- Modify: `collector/app.py`
- Modify: `collector/dashboard.html`

**Step 1: Implement parity**

Add Postgres tables and update leaderboard/dashboard logic to use the same company-paid cost formula and `subs` payload. Update the person-board UI to render subscription badges, relabel cost as `公司实付`, update the cost KPI meaning, and show governance unresolved count.

**Step 2: Run syntax and targeted checks**

Run:

```bash
python3 -m py_compile collector/dev_collector.py collector/app.py collector/subscriptions_sync.py
```

Expected: PASS.

### Task 4: Deploy Docs And Full Verification

**Files:**
- Modify: `deploy/RUNBOOK.md`
- Create: `deploy/subscriptions-sync.service`
- Create: `deploy/subscriptions-sync.timer`

**Step 1: Document and verify**

Document daily roster sync deployment and required env vars from `pipeline/.env`.

Run:

```bash
python3 -m pytest tests/ -q
python3 collector/subscriptions_sync.py --dry-run
python3 scripts/open_source_guard.py
python3 -m pytest tests/test_open_source_guard.py -q
```

Expected:
- full pytest PASS;
- dry-run without Feishu creds exits non-zero with a clear guard message;
- open-source guard PASS.
