# Production Brand Governance Metrics Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore production branding without committing private assets, and make the governance tab show metrics computed from the current SQLite data.

**Architecture:** The dashboard references only `/assets/company-logo.svg` and falls back to the neutral `ET` mark when the file is absent. The collector exposes `/v1/governance_metrics`, computed from existing `usage`, `people`, and `report_log` tables, with each metric marked `computed`, `partial`, or `pending`.

**Tech Stack:** Python 3.6-compatible stdlib HTTP server, SQLite, static HTML/CSS/JS dashboard, pytest.

### Task 1: Regression Tests

**Files:**
- Modify: `tests/test_governance_dashboard.py`
- Create: `tests/test_governance_metrics_api.py`

**Steps:**
1. Assert the dashboard references `/assets/company-logo.svg`, not `keep-logo.svg`.
2. Assert the dashboard fetches `/v1/governance_metrics` and stores `CACHE.governance`.
3. Build a temp SQLite database with representative `usage` and `report_log` rows.
4. Call `H._governance_metrics()` through a dummy handler and assert computed, partial, and pending metric classes.

### Task 2: Collector API

**Files:**
- Modify: `collector/dev_collector.py`

**Steps:**
1. Route `GET /v1/governance_metrics` to a new `_governance_metrics` method.
2. Compute lifetime, day-window, last-seven-day, source, client, and report-log summaries from SQLite.
3. Return seven stable metric IDs with real values where available and honest `partial`/`pending` availability where upstream data is missing.
4. Remove the duplicate `_meta` method definition.

### Task 3: Verify And Ship

**Files:**
- Modify: `AGENTS.md`

**Steps:**
1. Record the root cause under `Known gotchas`.
2. Run pytest, syntax compile, and the open-source guard.
3. Record ftask simulation/review.
4. Deploy only code and the generic production logo alias; do not overwrite production `.env`.
