# Windows Agent MDM Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a standalone Windows MDM deployment path that matches the current macOS tokreport flow without mixing the two push scripts.

**Architecture:** Windows uses separate PowerShell scripts. `mdm_bootstrap_windows.ps1` is the Windows MDM push/bootstrap entrypoint and installs/updates `%ProgramData%\TokReport\tokreport.ps1`; `tokreport_windows.ps1` runs under the logged-in user, calls tokscale, and posts the same `/v1/tokscale/report` payload used by macOS. The collector serves both `/tokreport.sh` and `/tokreport.ps1`, but MDM operators choose the OS-specific push script.

**Tech Stack:** PowerShell 5+, Windows Task Scheduler, Python stdlib collector, pytest.

### Task 1: Contract Tests

**Files:**
- Create: `tests/test_windows_agent_mdm.py`

**Step 1: Write failing tests**

Add tests that assert:
- `agent/tokreport_windows.ps1` exists and contains `/v1/tokscale/report`, `Get-CimInstance Win32_BIOS`, `Win32_BaseBoard`, `npx -y tokscale@latest`, `bunx tokscale@latest`, and no `Rename-Computer`.
- `agent/mdm_bootstrap_windows.ps1` exists and contains `Register-ScheduledTask`, `New-ScheduledTaskPrincipal`, `-GroupId`, `-LogonType Group`, `%ProgramData%` equivalent path, version gate, and `/tokreport.ps1` download validation.
- macOS `agent/mdm_bootstrap.sh` remains separate and does not contain Windows Task Scheduler code.
- `collector/dev_collector.py` serves `/tokreport.ps1`.
- README/help/architecture docs mention separate macOS and Windows MDM entrypoints.

**Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_windows_agent_mdm.py -q
```

Expected: FAIL because Windows scripts and collector route do not exist yet.

### Task 2: PowerShell Scripts and Collector Route

**Files:**
- Create: `agent/tokreport_windows.ps1`
- Create: `agent/mdm_bootstrap_windows.ps1`
- Modify: `collector/dev_collector.py`

**Step 1: Implement minimal scripts**

Create a reporter that:
- accepts `-Collector`, `-Token`, and `-Via mdm|manual`;
- picks a meaningful serial from BIOS first, then baseboard, rejecting empty/na/none/values with spaces;
- gathers hostname, OS caption/version, and IPv4 address;
- runs tokscale `models --json --no-spinner`, `monthly --json --no-spinner`, and `graph --since <date> --no-spinner`;
- posts `{serial,email:"",hostname,os,ip,via,models,monthly,graph}` to `/v1/tokscale/report`;
- exits `0` even on local collection/posting failures.

Create a standalone Windows bootstrap that:
- accepts `-Collector`, `-Token`, `-Version`, and optional `-InstallDir`;
- downloads `/tokreport.ps1` to `%ProgramData%\TokReport\tokreport.ps1`;
- only records the version after download validation succeeds;
- registers one `TokReport` scheduled task with logon and hourly triggers;
- uses a group principal so the task runs as the interactive logged-in user;
- starts the task once after registration.

Add `GET /tokreport.ps1` to the dev collector.

**Step 2: Run focused test**

Run:

```bash
pytest tests/test_windows_agent_mdm.py -q
```

Expected: PASS.

### Task 3: Docs

**Files:**
- Modify: `README.md`
- Modify: `README.en.md`
- Modify: `ARCHITECTURE.md`
- Modify: `DELIVERY.md`
- Modify: `collector/help.html`

**Step 1: Update docs**

Document:
- macOS path remains `agent/mdm_bootstrap.sh` + `/tokreport.sh` + LaunchAgent;
- Windows path is `agent/mdm_bootstrap_windows.ps1` + `/tokreport.ps1` + Task Scheduler;
- MDM executes `mdm_bootstrap_windows.ps1 -Collector https://<collector> -Token <token>`;
- manual fallback command for Windows users.

**Step 2: Run docs/guard tests**

Run:

```bash
pytest tests/test_windows_agent_mdm.py tests/test_governance_dashboard.py tests/test_open_source_guard.py -q
```

Expected: PASS.

### Task 4: Verification and ftask Gates

**Files:**
- Audit artifacts under `.ftask/windows-agent-mdm/`

**Step 1: Run focused/full tests**

Run:

```bash
pytest -q
python3 scripts/open_source_guard.py
```

Expected: PASS.

**Step 2: Capture simulations**

Use `ftask simulate windows-agent-mdm --capture ... -- <cmd>` for:
- focused pytest;
- open-source guard;
- a local collector static probe for `/tokreport.ps1` if a dev server is needed.

**Step 3: Review**

Run:

```bash
bun ~/.claude/PAI/TOOLS/ftask.ts review windows-agent-mdm
bun ~/.claude/PAI/TOOLS/ftask.ts review windows-agent-mdm --dispatch
```

Expected: no blocking concerns before reporting back.
