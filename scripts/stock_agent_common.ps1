# Stock Agent Windows runner - shared helpers (dot-sourced by the start / stop / check / install scripts).
# Nothing here reads or prints a secret: the .env file is passed to the Python process by PATH only.

Set-StrictMode -Version Latest

function Resolve-SAAppRoot([string]$AppRoot) {
    if ([string]::IsNullOrWhiteSpace($AppRoot)) { $AppRoot = Split-Path -Parent $PSScriptRoot }
    return (Resolve-Path -LiteralPath $AppRoot).Path
}

function Get-SAMainCheckout([string]$AppRoot) {
    # A git worktree has a .git FILE ("gitdir: <main>\.git\worktrees\<name>"); the main checkout owns the shared .venv and .env.
    $gitFile = Join-Path $AppRoot ".git"
    if (Test-Path -LiteralPath $gitFile -PathType Leaf) {
        $line = (Get-Content -LiteralPath $gitFile -TotalCount 1)
        if ($line -match '^gitdir:\s*(.+)$') {
            $gitdir = $Matches[1].Trim()
            $idx = $gitdir.Replace('/', '\').ToLower().LastIndexOf('\.git\worktrees\')
            if ($idx -gt 0) { return $gitdir.Replace('/', '\').Substring(0, $idx) }
        }
    }
    return $null
}

function Resolve-SAPython([string]$AppRoot, [string]$Python) {
    $candidates = @()
    if ($Python) { $candidates += $Python }
    if ($env:STOCK_AGENT_PYTHON) { $candidates += $env:STOCK_AGENT_PYTHON }
    $candidates += (Join-Path $AppRoot ".venv\Scripts\python.exe")
    $main = Get-SAMainCheckout $AppRoot
    if ($main) { $candidates += (Join-Path $main ".venv\Scripts\python.exe") }
    foreach ($c in $candidates) { if ($c -and (Test-Path -LiteralPath $c -PathType Leaf)) { return (Resolve-Path -LiteralPath $c).Path } }
    throw "No Python environment found. Looked for: $($candidates -join '; '). Pass -Python <path to python.exe>."
}

function Resolve-SAEnvFile([string]$AppRoot, [string]$EnvFile) {
    if ($EnvFile) {
        if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) { throw "Env file not found: $EnvFile" }
        return (Resolve-Path -LiteralPath $EnvFile).Path
    }
    $own = Join-Path $AppRoot ".env"
    if (Test-Path -LiteralPath $own -PathType Leaf) { return $own }
    $main = Get-SAMainCheckout $AppRoot
    if ($main) {
        $shared = Join-Path $main ".env"
        if (Test-Path -LiteralPath $shared -PathType Leaf) { return $shared }
    }
    return ""
}

function Get-SAPaths([string]$AppRoot, [string]$RuntimeDir, [string]$LogDir) {
    if (-not $RuntimeDir) { $RuntimeDir = Join-Path $AppRoot "runtime" }
    if (-not $LogDir) { $LogDir = Join-Path $AppRoot "logs" }
    return [pscustomobject]@{
        Runtime = $RuntimeDir; Logs = $LogDir
        Pid = (Join-Path $RuntimeDir "stock_agent.pid"); Stop = (Join-Path $RuntimeDir "stock_agent.stop")
        Stdout = (Join-Path $LogDir "stock_agent_stdout.log"); Stderr = (Join-Path $LogDir "stock_agent_stderr.log")
        Runner = (Join-Path $LogDir "stock_agent_runner.log")
    }
}

function Read-SAPidFile([string]$PidFile) {
    if (-not (Test-Path -LiteralPath $PidFile -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json) } catch { return [pscustomobject]@{ pid = -1; instance_id = ""; corrupt = $true } }
}

function Test-SAProcess($Info) {
    # True only when the PID is alive AND its command line is OUR runner with OUR instance id - never an arbitrary python.exe.
    if ($null -eq $Info -or -not $Info.pid -or [int]$Info.pid -le 0 -or -not $Info.instance_id) { return $false }
    $p = Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId = {0}" -f [int]$Info.pid) -ErrorAction SilentlyContinue
    if ($null -eq $p -or -not $p.CommandLine) { return $false }
    return ($p.CommandLine -like "*run_stock_agent.py*") -and ($p.CommandLine -like ("*" + $Info.instance_id + "*"))
}

function Get-SARunnerProcesses([string]$InstanceId) {
    if (-not $InstanceId) { return @() }
    return @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine -like "*run_stock_agent.py*" -and $_.CommandLine -like ("*" + $InstanceId + "*") })
}

function Write-SARunnerLog([string]$Path, [string]$Message) {
    try {
        $dir = Split-Path -Parent $Path
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        Add-Content -LiteralPath $Path -Value ("{0} {1}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"), $Message) -Encoding utf8
    } catch { }
}

function Invoke-SAGet([int]$Port, [string]$Path, [int]$TimeoutSec = 10) {
    return Invoke-RestMethod -Uri ("http://127.0.0.1:{0}{1}" -f $Port, $Path) -Method Get -TimeoutSec $TimeoutSec -UseBasicParsing
}
