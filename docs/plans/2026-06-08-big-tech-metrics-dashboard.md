# Big Tech Metrics Dashboard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the repo open-source-ready enough for public defaults and render a Meta/Google/Tesla-inspired governance metrics surface in the existing dashboard.

**Architecture:** Keep the current collector/dashboard split. Add a small static governance metric model rendered by `collector/dashboard.html`, document the model in `BIG-TECH-PATTERNS.md` and `ARCHITECTURE.md`, and enforce public hygiene with a Python guard script used by pytest and CLI.

**Tech Stack:** Python stdlib + pytest for guards, existing static HTML/CSS/JS for the frontend, existing Markdown docs.

### Task 1: Governance Dashboard Regression

**Files:**
- Create: `tests/test_governance_dashboard.py`
- Modify later: `collector/dashboard.html`

**Step 1: Write the failing test**

Assert that `collector/dashboard.html` contains a `data-t="governance"` tab, renders all required metric families, and keeps the Cursor range request suffix.

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_governance_dashboard.py -v`

Expected: FAIL because the governance tab and metric model do not exist yet.

**Step 3: Implement minimal frontend**

Add a governance tab, CSS for compact metric rows, a `GOVERNANCE_METRICS` array, and a `renderGovernance()` path in `render()`.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_governance_dashboard.py -v`

Expected: PASS.

### Task 2: Open Source Guard Regression

**Files:**
- Create: `scripts/open_source_guard.py`
- Create: `tests/test_open_source_guard.py`
- Modify later: docs and default sample data flagged by the guard

**Step 1: Write the failing test**

Add tests that run the guard on the repository and on a synthetic temp file containing a private email/IP to prove the guard catches real leaks.

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_open_source_guard.py -v`

Expected: FAIL because the guard does not exist.

**Step 3: Implement minimal guard**

Scan tracked text-like files, skip binary assets/cache/audit dirs, flag real employee email domains, private IPs, internal hostnames, SSH login forms, and selected real personal identifiers.

**Step 4: Sanitize public defaults**

Replace public sample data and docs with `Example Corp`, `example.com`, `collector.example.com`, and synthetic people. Keep internal integrations described as optional adapters.

**Step 5: Run guard test and CLI**

Run: `python3 -m pytest tests/test_open_source_guard.py -v` and `python3 scripts/open_source_guard.py`.

Expected: PASS and exit 0.

### Task 3: Documentation Mapping

**Files:**
- Modify: `BIG-TECH-PATTERNS.md`
- Modify: `ARCHITECTURE.md`
- Modify: `CODE-METRICS.md`
- Modify: `README.md`

**Step 1: Write doc assertions**

Extend `tests/test_governance_dashboard.py` or a dedicated doc test to assert Meta, Google/DORA, Google SRE, and Tesla mappings exist with source links.

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_governance_dashboard.py -v`

Expected: FAIL until docs are updated.

**Step 3: Update docs**

Document the metric families, source references, privacy/governance controls, and warning against individual performance scoring.

**Step 4: Verify**

Run: `python3 -m pytest`.

### Task 4: Full Verification

**Files:**
- No new files unless tests reveal gaps.

**Step 1: Compile Python**

Run: `python3 -m py_compile collector/app.py collector/dev_collector.py pipeline/build_report.py scripts/open_source_guard.py`

Expected: exit 0.

**Step 2: Run full tests**

Run: `python3 -m pytest`

Expected: all tests pass.

**Step 3: Browser check**

Start `collector/dev_collector.py` on the allocated `DEV_PORT`, open the dashboard, click “治理指标”, and verify desktop/mobile layout is readable.

**Step 4: Record state**

Run `ftask state big-tech-metrics-dashboard --note "...verification summary..."`.
