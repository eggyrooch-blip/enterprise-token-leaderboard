import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORTER = ROOT / "agent" / "tokreport_windows.ps1"
BOOTSTRAP = ROOT / "agent" / "mdm_bootstrap_windows.ps1"
MAC_BOOTSTRAP = ROOT / "agent" / "mdm_bootstrap.sh"
COLLECTOR = ROOT / "collector" / "dev_collector.py"
DEPLOY = ROOT / "deploy" / "deploy.sh"
README = ROOT / "README.md"
README_EN = ROOT / "README.en.md"
ARCHITECTURE = ROOT / "ARCHITECTURE.md"
DELIVERY = ROOT / "DELIVERY.md"
HELP = ROOT / "collector" / "help.html"


def test_windows_reporter_collects_serial_and_posts_existing_tokscale_payload():
    script = REPORTER.read_text(encoding="utf-8")

    assert "Get-CimInstance" in script
    assert "Win32_BIOS" in script
    assert "Win32_BaseBoard" in script
    assert "IsMeaningfulSerial" in script
    assert "/v1/tokscale/report" in script
    assert "models --json --no-spinner" in script
    assert "monthly --json --no-spinner" in script
    assert "graph --since" in script
    assert "npx -y tokscale@latest" in script
    assert "bunx tokscale@latest" in script
    assert ".cmd" in script
    assert ".ps1" in script
    assert "$env:ComSpec" in script
    assert '@("/d", "/c", $cmdLine)' in script
    assert '"/s"' not in script
    assert "powershell.exe" in script
    assert "Rename-Computer" not in script


def test_windows_reporter_writes_programdata_log_for_mdm_debugging():
    script = REPORTER.read_text(encoding="utf-8")

    assert "tokreport.log" in script
    assert "Add-Content -LiteralPath $LogPath" in script
    assert "New-Item -ItemType Directory -Path $LogDir" in script


def test_windows_reporter_avoids_invalid_variable_colon_interpolation():
    script = REPORTER.read_text(encoding="utf-8")
    scoped = {"env", "script", "global", "local", "private", "using"}
    invalid = [
        match.group(0)
        for match in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_]*):", script)
        if match.group(1) not in scoped
    ]

    assert invalid == []


def test_windows_bootstrap_is_standalone_logged_in_user_scheduled_task():
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert "$env:ProgramData" in script
    assert "[int]$Version = 5" in script
    assert "tokreport.ps1" in script
    assert "/tokreport.ps1" in script
    assert "Register-ScheduledTask" in script
    assert "New-ScheduledTaskPrincipal" in script
    assert "-GroupId" in script
    assert "-LogonType Group" not in script
    assert "New-ScheduledTaskTrigger -AtLogOn" in script
    assert "New-ScheduledTaskTrigger -Once" in script
    assert "-RepetitionInterval" in script
    assert "-RepetitionDuration" in script
    assert ".Repetition.Interval" not in script
    assert ".Repetition.Duration" not in script
    assert "-NonInteractive" in script
    assert "-WindowStyle Hidden" in script
    assert ".version" in script
    assert "v1/tokscale/report" in script
    assert "LaunchAgent" not in script


def test_windows_bootstrap_starts_existing_task_when_already_current():
    script = BOOTSTRAP.read_text(encoding="utf-8")
    already_current_block = script[
        script.index("if ((Test-Path -LiteralPath $versionPath)") :
        script.index("$fresh = $false")
    ]

    assert "Start-ScheduledTask -TaskName $TaskName" in already_current_block
    assert "started existing Scheduled Task" in already_current_block


def test_windows_bootstrap_logs_scheduled_task_diagnostics_after_start():
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert "Get-ScheduledTaskInfo -TaskName $TaskName" in script
    assert "tokreport.log" in script
    assert "Get-Content -LiteralPath $logPath -Tail" in script
    assert "LastRunTime" in script
    assert "LastTaskResult" in script
    assert "State" in script
    assert "task info unavailable" in script


def test_macos_bootstrap_remains_separate_from_windows_push_script():
    script = MAC_BOOTSTRAP.read_text(encoding="utf-8")

    assert "LaunchAgent" in script
    assert "launchctl" in script
    assert "Register-ScheduledTask" not in script
    assert "New-ScheduledTaskPrincipal" not in script


def test_collector_serves_windows_reporter_next_to_macos_reporter():
    source = COLLECTOR.read_text(encoding="utf-8")

    assert "remote_tokscale_report.sh" in source
    assert "tokreport_windows.ps1" in source
    assert "X-Forwarded-Proto" in source
    assert 'TOKEN="${TOKEN:-' in source
    assert 'path == "/tokreport.sh"' in source
    assert 'path == "/tokreport.ps1"' in source


def test_deploy_syncs_reporter_scripts_needed_by_static_routes():
    script = DEPLOY.read_text(encoding="utf-8")

    assert "collector/help.html" in script
    assert "agent/remote_tokscale_report.sh" in script
    assert "agent/tokreport_windows.ps1" in script
    assert "tokreport.ps1" in script
    assert "${REMOTE_DIR}/tokreport.sh" not in script


def test_docs_describe_separate_windows_mdm_without_removing_macos_path():
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (README, README_EN, ARCHITECTURE, DELIVERY, HELP)
    )

    assert "agent/mdm_bootstrap.sh" in text
    assert "tokreport.sh" in text
    assert "LaunchAgent" in text
    assert "mdm_bootstrap_windows.ps1" in text
    assert "tokreport.ps1" in text
    assert "Task Scheduler" in text
    assert "Scheduled Task" in text
