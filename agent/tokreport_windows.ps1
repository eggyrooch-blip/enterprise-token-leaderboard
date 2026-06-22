param(
    [string]$Collector = $env:COLLECTOR,
    [string]$Token = $env:TOKEN,
    [ValidateSet("mdm", "manual")]
    [string]$Via = "mdm",
    [string]$ConfigPath = (Join-Path $env:ProgramData "TokReport\tokreport.config.json")
)

# Windows tokreport runner. Intended to run as the logged-in user from Task
# Scheduler so tokscale reads that user's local Claude/Codex/Cursor/Gemini data.
# It only uploads aggregate token/cost JSON to /v1/tokscale/report.

$ErrorActionPreference = "Stop"
$ScriptTimeoutSeconds = 600
$CommandTimeoutSeconds = 180
$StartedAt = Get-Date
$TmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("tokreport-" + [guid]::NewGuid().ToString("N"))
$LogDir = Join-Path $env:ProgramData "TokReport"
$LogPath = Join-Path $LogDir "tokreport.log"

function Log {
    param([string]$Message)
    $line = "[tokreport-windows] $((Get-Date).ToString('s')) $Message"
    # 必须用 Write-Host,绝不能 Write-Output:Write-Output 会把日志行注入调用它的
    # 函数返回值(success 流)。Get-DeviceSerial 内部调用 Log,曾导致 $serial 变成
    # [日志行..., 真SN] 这样的数组,上报到服务端 serial 成 list → 500,Windows 机器
    # 全部进不了榜。Write-Host 走 host/information 流,不污染任何函数返回值。
    Write-Host $line
    try {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
        Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value $line
    } catch {}
}

function Load-Config {
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        return
    }
    try {
        $cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]::IsNullOrWhiteSpace($Collector) -and $cfg.Collector) {
            $script:Collector = [string]$cfg.Collector
        }
        if ([string]::IsNullOrWhiteSpace($Token) -and $cfg.Token) {
            $script:Token = [string]$cfg.Token
        }
    } catch {
        Log "config ignored: $($_.Exception.Message)"
    }
}

function IsMeaningfulSerial {
    param([AllowNull()][string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $false
    }
    $v = $Value.Trim()
    if ($v -match "\s") {
        return $false
    }
    $compact = ($v -replace "[\s\.\-_/]", "").ToLowerInvariant()
    $bad = @(
        "na", "n/a", "none", "null", "unknown", "defaultstring",
        "tobefilledbyoem", "systemserialnumber", "serialnumber"
    )
    return -not ($bad -contains $compact)
}

function Get-CimValue {
    param([string]$ClassName, [string]$Property)
    try {
        $obj = Get-CimInstance -ClassName $ClassName -ErrorAction Stop | Select-Object -First 1
        return [string]$obj.$Property
    } catch {
        try {
            $obj = Get-WmiObject -Class $ClassName -ErrorAction Stop | Select-Object -First 1
            return [string]$obj.$Property
        } catch {
            return ""
        }
    }
}

function Get-DeviceSerial {
    $biosSn = Get-CimValue -ClassName "Win32_BIOS" -Property "SerialNumber"
    Log "The original serial number of BIOS is $biosSn"
    $baseboardSn = Get-CimValue -ClassName "Win32_BaseBoard" -Property "SerialNumber"
    Log "The original serial number of baseboard is $baseboardSn"

    if (IsMeaningfulSerial $biosSn) {
        Log "The meaningful SN should be $biosSn, which is directly retrieved from the BIOS serial number."
        return $biosSn.Trim()
    }
    if (IsMeaningfulSerial $baseboardSn) {
        Log "The meaningful SN should be $baseboardSn, which is directly retrieved from the baseboard serial number."
        return $baseboardSn.Trim()
    }
    Log "The serial numbers of BIOS and baseboard are both meaningless therefore serial is empty."
    return ""
}

function Get-PrimaryIPv4 {
    try {
        $ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object {
                $_.IPAddress -ne "127.0.0.1" -and
                $_.IPAddress -notlike "169.254.*" -and
                $_.PrefixOrigin -ne "WellKnown"
            } |
            Select-Object -First 1 -ExpandProperty IPAddress
        if ($ip) {
            return [string]$ip
        }
    } catch {}
    return "unknown"
}

function Get-OSLabel {
    try {
        $os = Get-CimInstance -ClassName "Win32_OperatingSystem" -ErrorAction Stop
        return (($os.Caption, $os.Version) -join " ").Trim()
    } catch {
        return [System.Environment]::OSVersion.VersionString
    }
}

function Ensure-Bun {
    # 没有任何 JS 运行时(node/npx/bun)时,静默安装 Bun 到用户目录后返回 bun.exe 路径;
    # 装不上返回 $null。设计要点(对齐"员工无感知"):
    #   - 用户级安装,装进 %USERPROFILE%\.bun,【不需要管理员】(计划任务就是普通用户身份跑)
    #   - 无弹窗:子 powershell -WindowStyle Hidden -NonInteractive 跑官方安装脚本
    #   - 幂等:bun.exe 已存在直接复用,绝不重复下载
    #   - 硬超时 + 全程 try/catch:装不上(如公司网络挡了 bun.sh)就返回 $null,
    #     调用方退回发空数据,绝不让安装失败把上报打挂
    $bunBin = Join-Path $env:USERPROFILE ".bun\bin"
    $bunExe = Join-Path $bunBin "bun.exe"
    if (Test-Path -LiteralPath $bunExe) {
        return $bunExe
    }
    # 预算保护:安装可能要拉几十 MB,留足时间,时间不够这轮就不装(下轮再来)
    if (((Get-Date) - $StartedAt).TotalSeconds -gt ($ScriptTimeoutSeconds - 200)) {
        Log "skip bun install (insufficient time budget this run)"
        return $null
    }
    Log "no JS runtime found; installing Bun silently to user profile"
    try {
        $installer = Join-Path $TmpDir "bun-install.ps1"
        Invoke-WebRequest -UseBasicParsing -Uri "https://bun.sh/install.ps1" `
            -OutFile $installer -TimeoutSec 30
        $p = Start-Process -FilePath "powershell.exe" `
            -ArgumentList @("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $installer) `
            -WindowStyle Hidden -PassThru
        if (-not $p.WaitForExit(180000)) {
            try { $p.Kill() } catch {}
            Log "bun install timed out"
            return $null
        }
    } catch {
        Log "bun install failed: $($_.Exception.Message)"
        return $null
    }
    if (Test-Path -LiteralPath $bunExe) {
        Log "Bun installed to $bunBin"
        return $bunExe
    }
    Log "bun install ran but bun.exe not found"
    return $null
}

function Get-TokscaleCommand {
    $local = Get-Command "tokscale" -ErrorAction SilentlyContinue
    if ($local) {
        Log "runtime: local tokscale found at $($local.Source)"
        return @{ File = $local.Source; Prefix = @() }
    }
    $npx = Get-Command "npx" -ErrorAction SilentlyContinue
    if ($npx) {
        # Get-Command often resolves to npx.ps1. If the path lives under
        # C:\Program Files, powershell -File can split the unquoted path.
        # Prefer the sibling npx.cmd and route through the cmd branch.
        $npxFile = $npx.Source
        if ($npxFile -like '*.ps1') {
            $cmd = [System.IO.Path]::ChangeExtension($npxFile, 'cmd')
            if (Test-Path -LiteralPath $cmd) { $npxFile = $cmd }
        }
        Log "runtime: no tokscale, using npx ($npxFile) -> tokscale@latest"
        # Fallback command equivalent: npx -y tokscale@latest ...
        return @{ File = $npxFile; Prefix = @("-y", "tokscale@latest") }
    }
    $bunx = Get-Command "bunx" -ErrorAction SilentlyContinue
    if ($bunx) {
        Log "runtime: no tokscale, using bunx ($($bunx.Source)) -> tokscale@latest"
        # Fallback command equivalent: bunx tokscale@latest ...
        return @{ File = $bunx.Source; Prefix = @("tokscale@latest") }
    }
    # 三者皆无(典型财务/非开发 Windows):静默装 Bun,再用 `bun x` 跑 tokscale。
    Log "runtime: none of tokscale/npx/bunx present -> will auto-install Bun"
    $bunExe = Ensure-Bun
    if ($bunExe) {
        Log "runtime: using freshly-handled bun ($bunExe) x tokscale@latest"
        # `bun x tokscale@latest ...` 等价于 bunx;用显式 bun.exe 路径,免依赖 PATH 刷新。
        return @{ File = $bunExe; Prefix = @("x", "tokscale@latest") }
    }
    Log "runtime: NO usable runtime and Bun install failed -> will send empty data"
    return $null
}

function Read-CleanJson {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    $idx = $raw.IndexOf("{")
    if ($idx -ge 0) {
        return $raw.Substring($idx)
    }
    return ""
}

function Quote-CmdArg {
    param([string]$Arg)
    if ($Arg -notmatch '[\s&()^=;!+,`~\[\]{}]') {
        return $Arg
    }
    return '"' + ($Arg -replace '"', '""') + '"'
}

function Start-CapturedProcess {
    param(
        [string]$File,
        [string[]]$Arguments,
        [string]$OutPath,
        [string]$ErrPath
    )
    $ext = [System.IO.Path]::GetExtension($File).ToLowerInvariant()
    if (@(".cmd", ".bat") -contains $ext) {
        $cmdExe = if ($env:ComSpec) { $env:ComSpec } else { "cmd.exe" }
        $cmdLine = '"' + (Quote-CmdArg $File) + " " + (($Arguments | ForEach-Object { Quote-CmdArg $_ }) -join " ") + '"'
        return Start-Process -FilePath $cmdExe -ArgumentList @("/d", "/c", $cmdLine) `
            -RedirectStandardOutput $OutPath -RedirectStandardError $ErrPath `
            -NoNewWindow -PassThru
    }
    if ($ext -eq ".ps1") {
        $psExe = "powershell.exe"
        $found = Get-Command "powershell.exe" -ErrorAction SilentlyContinue
        if ($found) {
            $psExe = $found.Source
        }
        $psArgs = @("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", ('"' + $File + '"')) + $Arguments
        return Start-Process -FilePath $psExe -ArgumentList $psArgs `
            -RedirectStandardOutput $OutPath -RedirectStandardError $ErrPath `
            -NoNewWindow -PassThru
    }
    return Start-Process -FilePath $File -ArgumentList $Arguments `
        -RedirectStandardOutput $OutPath -RedirectStandardError $ErrPath `
        -NoNewWindow -PassThru
}

function Invoke-TokscaleJson {
    param(
        [string]$Display,
        [string[]]$Arguments,
        [string]$Needle,
        [string]$FallbackJson
    )
    $spec = Get-TokscaleCommand
    if (-not $spec) {
        Log "tokscale unavailable for $Display"
        return $FallbackJson
    }

    for ($try = 1; $try -le 3; $try++) {
        if (((Get-Date) - $StartedAt).TotalSeconds -gt $ScriptTimeoutSeconds) {
            Log "script timeout before $Display"
            return $FallbackJson
        }

        $out = Join-Path $TmpDir ("tokscale-$($Display.Split(' ')[0])-$try.json")
        $err = Join-Path $TmpDir ("tokscale-$($Display.Split(' ')[0])-$try.err")
        $allArgs = @($spec.Prefix) + $Arguments
        try {
            $p = Start-CapturedProcess -File $spec.File -Arguments $allArgs -OutPath $out -ErrPath $err
            if (-not $p.WaitForExit($CommandTimeoutSeconds * 1000)) {
                try { $p.Kill() } catch {}
                Log "$Display timed out on try $try"
                continue
            }
            $json = Read-CleanJson -Path $out
            if ($json -and $json.Contains($Needle)) {
                Log "${Display}: ok (valid json, $($json.Length) chars)"
                return $json
            }
            # 没拿到含 Needle 的 JSON:把 stderr 头部记下来,直说为啥(命令找不到/tokscale 报错等)
            $errHead = ""
            if (Test-Path -LiteralPath $err) {
                $errHead = (Get-Content -LiteralPath $err -Raw -ErrorAction SilentlyContinue)
            }
            if ($errHead) { $errHead = ($errHead -replace "\s+", " ").Trim() }
            if ($errHead.Length -gt 200) { $errHead = $errHead.Substring(0, 200) }
            Log "${Display}: no valid json on try $try (stderr: $errHead)"
        } catch {
            Log "$Display failed on try $($try): $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 2
    }
    return $FallbackJson
}

function Convert-JsonOrFallback {
    param([string]$Json, [string]$FallbackJson)
    try {
        return $Json | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return $FallbackJson | ConvertFrom-Json
    }
}

try {
    Load-Config
    if ([string]::IsNullOrWhiteSpace($Collector) -or $Collector -like "*example.com*") {
        Log "COLLECTOR is required"
        exit 0
    }
    if ([string]::IsNullOrWhiteSpace($Token)) {
        Log "TOKEN is required"
        exit 0
    }

    New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null

    $collectorBase = $Collector.TrimEnd("/")
    $serial = Get-DeviceSerial
    $hostname = $env:COMPUTERNAME
    if ([string]::IsNullOrWhiteSpace($hostname)) {
        $hostname = [System.Net.Dns]::GetHostName()
    }
    $osLabel = Get-OSLabel
    $ip = Get-PrimaryIPv4
    $since = (Get-Date).AddDays(-100).ToString("yyyy-MM-dd")

    $modelsJson = Invoke-TokscaleJson `
        -Display "models --json --no-spinner" `
        -Arguments @("models", "--json", "--no-spinner") `
        -Needle '"entries"' `
        -FallbackJson '{"entries":[]}'
    $monthlyJson = Invoke-TokscaleJson `
        -Display "monthly --json --no-spinner" `
        -Arguments @("monthly", "--json", "--no-spinner") `
        -Needle '"entries"' `
        -FallbackJson '{"entries":[]}'
    $graphJson = Invoke-TokscaleJson `
        -Display "graph --since $since --no-spinner" `
        -Arguments @("graph", "--since", $since, "--no-spinner") `
        -Needle '"contributions"' `
        -FallbackJson '{"contributions":[]}'

    $payload = [ordered]@{
        serial = $serial
        email = ""
        hostname = $hostname
        os = $osLabel
        ip = $ip
        via = $Via
        models = Convert-JsonOrFallback -Json $modelsJson -FallbackJson '{"entries":[]}'
        monthly = Convert-JsonOrFallback -Json $monthlyJson -FallbackJson '{"entries":[]}'
        graph = Convert-JsonOrFallback -Json $graphJson -FallbackJson '{"contributions":[]}'
    }
    $body = $payload | ConvertTo-Json -Depth 100 -Compress
    $headers = @{ Authorization = "Bearer $Token" }
    $resp = Invoke-RestMethod -Method Post -Uri "$collectorBase/v1/tokscale/report" `
        -Headers $headers -ContentType "application/json; charset=utf-8" `
        -Body $body -TimeoutSec 20
    Log "tokreport OK: serial=$serial user=$env:USERNAME resp=$(($resp | ConvertTo-Json -Compress -Depth 8))"
} catch {
    Log "tokreport SENT (unverified): $($_.Exception.Message)"
} finally {
    try { Remove-Item -LiteralPath $TmpDir -Recurse -Force -ErrorAction SilentlyContinue } catch {}
}

exit 0
