param(
    [string]$Collector = $env:COLLECTOR,
    [string]$Token = $env:TOKEN,
    [int]$Version = 4,
    [string]$InstallDir = (Join-Path $env:ProgramData "TokReport"),
    [string]$TaskName = "TokReport"
)

# Standalone Windows MDM push/bootstrap script.
# Run this from MDM as Administrator/SYSTEM. It installs a separate Windows
# tokreport.ps1 and registers one Scheduled Task that runs as the logged-in user.

$ErrorActionPreference = "Stop"

function Log {
    param([string]$Message)
    Write-Output "[tokreport-windows-bootstrap] $Message"
}

function Get-TaskIfExists {
    try {
        return Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    } catch {
        return $null
    }
}

function Log-TaskInfo {
    try {
        Start-Sleep -Seconds 5
        $taskNow = Get-TaskIfExists
        $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
        $state = if ($taskNow) { [string]$taskNow.State } else { "unknown" }
        Log "task state=$state lastRunTime=$($info.LastRunTime) lastTaskResult=$($info.LastTaskResult)"
        $logPath = Join-Path $InstallDir "tokreport.log"
        if (Test-Path -LiteralPath $logPath) {
            Log "tokreport.log tail:"
            Get-Content -LiteralPath $logPath -Tail 40 -Encoding UTF8 |
                ForEach-Object { Log "  $_" }
        } else {
            Log "tokreport.log not found yet"
        }
    } catch {
        Log "task info unavailable: $($_.Exception.Message)"
    }
}

try {
    if ([string]::IsNullOrWhiteSpace($Collector) -or $Collector -like "*example.com*") {
        Log "COLLECTOR is required; pass -Collector https://<collector>"
        exit 0
    }
    if ([string]::IsNullOrWhiteSpace($Token)) {
        Log "TOKEN is required; pass -Token <bearer-token>"
        exit 0
    }

    $collectorBase = $Collector.TrimEnd("/")
    $scriptPath = Join-Path $InstallDir "tokreport.ps1"
    $configPath = Join-Path $InstallDir "tokreport.config.json"
    $versionPath = Join-Path $InstallDir ".version"
    $downloadUrl = "$collectorBase/tokreport.ps1"

    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    @{ Collector = $collectorBase; Token = $Token } |
        ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath $configPath -Encoding UTF8

    $task = Get-TaskIfExists
    if ((Test-Path -LiteralPath $versionPath) -and
        ((Get-Content -LiteralPath $versionPath -Raw).Trim() -eq [string]$Version) -and
        (Test-Path -LiteralPath $scriptPath) -and
        $task) {
        try {
            Start-ScheduledTask -TaskName $TaskName
            Log "already v$Version and Scheduled Task exists; started existing Scheduled Task"
        } catch {
            Log "already v$Version and Scheduled Task exists; immediate start skipped: $($_.Exception.Message)"
        }
        Log-TaskInfo
        exit 0
    }

    $fresh = $false
    $tmp = Join-Path $InstallDir (".dl." + [guid]::NewGuid().ToString("N") + ".ps1")
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $downloadUrl -OutFile $tmp -TimeoutSec 30
        $downloaded = Get-Content -LiteralPath $tmp -Raw -Encoding UTF8
        if ($downloaded -match "v1/tokscale/report" -and $downloaded -match "param\s*\(") {
            Move-Item -LiteralPath $tmp -Destination $scriptPath -Force
            $fresh = $true
            Log "downloaded $downloadUrl"
        } else {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
            Log "download validation failed: $downloadUrl"
        }
    } catch {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        Log "download failed: $($_.Exception.Message)"
    }

    if (-not (Test-Path -LiteralPath $scriptPath)) {
        Log "no local reporter script to fall back on; abort"
        exit 0
    }

    $actionArg = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`" -ConfigPath `"$configPath`" -Via mdm"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArg
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn
    $hourlyTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) `
        -RepetitionInterval (New-TimeSpan -Hours 1) `
        -RepetitionDuration (New-TimeSpan -Days 9999)
    $principal = New-ScheduledTaskPrincipal -GroupId "S-1-5-32-545" -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
        -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Trigger @($logonTrigger, $hourlyTrigger) `
        -Principal $principal -Settings $settings -Force | Out-Null

    try {
        Start-ScheduledTask -TaskName $TaskName
    } catch {
        Log "task registered; immediate start skipped: $($_.Exception.Message)"
    }
    Log-TaskInfo

    if ($fresh) {
        Set-Content -LiteralPath $versionPath -Encoding ASCII -Value ([string]$Version)
        Log "installed v$Version; Scheduled Task active"
    } else {
        Log "Scheduled Task active, but script not refreshed; version NOT bumped"
    }
} catch {
    Log "bootstrap failed but exits cleanly: $($_.Exception.Message)"
}

exit 0
