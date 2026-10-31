<#
.SYNOPSIS
  Stock Agent - Market Morning: the safe weekday start of the LIVE Stock Agent app/server (Alpaca PAPER, Monitor only).

.DESCRIPTION
  Run by the Windows Scheduled Task "Stock Agent - Market Morning" (Monday-Friday 06:15 America/Los_Angeles; the installer
  scripts\install_market_morning_task.ps1 adds an at-logon and an on-resume trigger). It starts the app/server ONLY. It never
  enables Paper Auto, never calls /api/execution/auto/enable (it sends no POST at all), never changes the mode and never places an
  order: its only HTTP requests are GETs to 127.0.0.1. The server comes up in MONITOR_ONLY by itself (Stage 6.10A MANUAL_ARM: a
  persisted Paper Auto is demoted before the executor starts), then runs its startup preflight, reconciliation and account refresh.
  A person checks the dashboard and arms Paper Auto there.

  This is the version-controlled copy of the wrapper that used to live only in <live>\runtime. Behaviour is unchanged; every
  hard-coded path is a parameter whose default is today's live value. Two hardenings: the very first statement writes a "launch"
  line to the log (before any file, environment or process is read), and a script-scope trap turns every unexpected terminating
  error into a logged RESULT line with a non-zero exit - a task that ends without a RESULT line was killed from outside
  (Task Scheduler reports 0xC000013A when it ends a task at its time limit or on End Task).

  Steps, each logged to logs\market_morning.log (-LogFile):
    1. Canonical live path: this file must sit in <live>\scripts or <live>\runtime, <live> must be the stock-agent-live worktree
       (its leaf name), and the deployed code must carry the Stage 6.10A startup demotion (autorun\service.py). Older code resumed a
       persisted Paper Auto: never auto-start it.
    2. Paper environment: EXECUTION_BROKER resolves to alpaca_paper, execution\broker.py pins PAPER_HOST = paper-api.alpaca.markets, and
       the paper credentials are set (checked by NAME; no secret value is kept or logged).
    3. Config: PAPER_AUTO_RUN_POLICY resolves to MANUAL_ARM, PAPER_AUTO_SCHEDULED_ARM_ENABLED to false, STOCK_AGENT_ENV is not a test value.
       Values resolve like the runner: overrides file, then process environment, then the shared .env, then the code default.
    4. Duplicate guard: a verified live runner (PID file + its command line), any run_stock_agent.py process of this app root, or our
       runner listening on the port means the server already runs: nothing is started; its state is read (GET) and logged. A port held
       by anything else is a failure.
    5. Start through the canonical scripts\start_stock_agent.ps1. It starts the server detached through WMI, so this task never owns the
       server process (ending or timing out the task can never stop the bot), refuses a duplicate, and waits for /api/health.
    6. Read back (GET /api/execution/activity): mode, auto-run policy, startup outcome, startup preflight, forward cohort. Logged.

  -DryRun runs steps 1-4 and starts nothing.

  Exit codes: 0 = running (started now in Monitor only, or already running; nothing changed)
              1 = a check failed or an unexpected error; nothing was started
              2 = the start failed
              3 = started, but the read-back state was unexpected (logged; this script changed nothing)
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [int]$ReadBackSeconds = 150,
    [string]$Live = "C:\Data Analyst Py\DA IDX\stock-agent-live",
    [string]$MainCheckout = "C:\Data Analyst Py\DA IDX\stock-agent",
    [string]$Python = "",
    [string]$EnvFile = "",
    [string]$Overrides = "",
    [string]$DbPath = "",
    [int]$Port = 8000,
    [string]$LogFile = ""
)
# ---- launch line first: nothing below runs before this is on disk --------------------------------------------------------------------
$RunId = [guid]::NewGuid().ToString("N").Substring(0, 8)
$Tag = $(if ($DryRun) { " DRY-RUN" } else { "" })
if (-not $LogFile) { $LogFile = Join-Path $Live "logs\market_morning.log" }
function Log([string]$Message) {
    $line = "{0} [{1}]{2} {3}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"), $RunId, $Tag, $Message
    try { [Console]::Out.WriteLine($line) } catch { }
    try {
        $dir = Split-Path -Parent $LogFile
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        Add-Content -LiteralPath $LogFile -Value $line -Encoding utf8
    } catch { }
}
Log ("launch (pid {0}, user {1}\{2}, session {3}, dry-run {4}, wrapper {5})" -f $PID, $env:USERDOMAIN, $env:USERNAME,
    $(try { (Get-Process -Id $PID).SessionId } catch { "?" }), [bool]$DryRun, $PSCommandPath)
function Finish([int]$Code, [string]$Message) {
    Log ("RESULT: {0} (exit {1})" -f $Message, $Code)
    exit $Code
}
trap {
    $where = $(try { $_.InvocationInfo.ScriptLineNumber } catch { "?" })
    Log ("RESULT: unexpected error at line {0}: {1} (exit 1)" -f $where, $_.Exception.Message)
    exit 1
}
$ErrorActionPreference = "Stop"

if (-not $Python) { $Python = Join-Path $MainCheckout ".venv\Scripts\python.exe" }
if (-not $EnvFile) { $EnvFile = Join-Path $MainCheckout ".env" }
if (-not $Overrides) { $Overrides = Join-Path $Live "runtime\live_paper.env" }
if (-not $DbPath) { $DbPath = Join-Path $Live "data\stock_agent.db" }
$LiveLeaf = Split-Path -Leaf $Live

function Truthy([string]$Value) { return @("1", "true", "yes", "on") -contains ([string]$Value).Trim().ToLower() }

# Settings whose VALUES may be kept; for every other key only "set / not set" is recorded (secrets never leave the file reader).
$Visible = @("EXECUTION_BROKER", "PAPER_AUTO_RUN_POLICY", "PAPER_AUTO_SCHEDULED_ARM_ENABLED", "STOCK_AGENT_ENV", "AUTORUN_SUPERVISOR_ENABLED",
             "EXECUTION_SCHEDULER_ENABLED", "UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED")
function Read-DotEnv([string]$Path) {
    $h = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $h }
    foreach ($raw in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $l = $raw.Trim()
        if (-not $l -or $l.StartsWith("#")) { continue }
        if ($l.StartsWith("export ")) { $l = $l.Substring(7).Trim() }
        $i = $l.IndexOf("=")
        if ($i -lt 1) { continue }
        $k = $l.Substring(0, $i).Trim()
        $v = $l.Substring($i + 1).Trim()
        if ($v.Length -ge 2 -and (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'")))) { $v = $v.Substring(1, $v.Length - 2) }
        elseif ($v -match '^(.*?)\s+#') { $v = $Matches[1] }
        if ($Visible -contains $k) { $h[$k] = $v } else { $h[$k] = $(if ($v) { "<set>" } else { "" }) }
    }
    return $h
}
$EnvValues = Read-DotEnv $EnvFile
$OverrideValues = Read-DotEnv $Overrides
function Resolve-Setting([string]$Key, [string]$Default) {
    if ($OverrideValues.ContainsKey($Key)) { return [string]$OverrideValues[$Key] }                   # load_dotenv(overrides, override=True)
    $p = [Environment]::GetEnvironmentVariable($Key)
    if ($null -ne $p) { return $(if ($Visible -contains $Key) { $p } elseif ($p) { "<set>" } else { "" }) }   # the process wins over .env
    if ($EnvValues.ContainsKey($Key)) { return [string]$EnvValues[$Key] }
    return $Default
}

function Read-State {
    # GET only. Waits until the activity view reports the auto-run block (and, best effort, the first preflight).
    $deadline = (Get-Date).AddSeconds($ReadBackSeconds)
    $act = $null
    while ((Get-Date) -lt $deadline) {
        try { $act = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/execution/activity" -f $Port) -Method Get -TimeoutSec 60 -UseBasicParsing } catch { $act = $null }
        if ($act -and $act.auto_run -and $act.auto_run.preflight -and $act.worker -and $act.worker.state -ne "STARTING") { break }
        Start-Sleep -Seconds 5
    }
    return $act
}
function Describe($act) {
    if (-not $act) { return "state unreadable (no answer from GET /api/execution/activity)" }
    $ar = $act.auto_run
    $pf = $(if ($ar -and $ar.preflight) { "{0} at {1}{2}" -f $ar.preflight.status, $ar.preflight.at, $(if (@($ar.preflight.blocking).Count) { " blocking: " + (@($ar.preflight.blocking) -join ",") } else { "" }) } else { "not computed yet" })
    $st = $(if ($ar -and $ar.startup) { "demoted={0} code={1} passive={2}" -f $ar.startup.demoted, $ar.startup.code, $ar.startup.passive } else { "n/a" })
    $co = $(if ($act.forward_session -and $act.forward_session.cohort) { "{0} {1}" -f $act.forward_session.cohort.label, $act.forward_session.cohort.cohort_id } else { "none yet" })
    return ("mode={0} | auto-run policy={1} scheduled_arm_enabled={2} | startup: {3} | preflight: {4} | worker={5} | forward cohort: {6} | broker_calls={7}" -f `
        $act.mode.mode, $(if ($ar) { $ar.policy } else { "?" }), $(if ($ar) { $ar.scheduled_arm_enabled } else { "?" }), $st, $pf, $act.worker.state, $co, $act.broker_calls)
}

Log ("start (user {0}\{1}, host {2})" -f $env:USERDOMAIN, $env:USERNAME, $env:COMPUTERNAME)

# ---- 1. canonical live path ------------------------------------------------------------------------------------------------------------
$here = (Resolve-Path -LiteralPath $PSScriptRoot).Path
if ($here -ne (Join-Path $Live "scripts") -and $here -ne (Join-Path $Live "runtime")) { Finish 1 ("this wrapper must run from {0}\scripts or {0}\runtime, not {1}" -f $Live, $here) }
$gitFile = Join-Path $Live ".git"
if (-not (Test-Path -LiteralPath $gitFile -PathType Leaf) -or ((Get-Content -LiteralPath $gitFile -TotalCount 1) -notmatch ('worktrees[\\/]' + [regex]::Escape($LiveLeaf) + '\s*$'))) {
    Finish 1 "$Live is not the $LiveLeaf worktree"
}
foreach ($f in @("scripts\start_stock_agent.ps1", "scripts\run_stock_agent.py", "scripts\stock_agent_common.ps1", "execution\broker.py", "api\server.py", "autorun\service.py")) {
    if (-not (Test-Path -LiteralPath (Join-Path $Live $f) -PathType Leaf)) { Finish 1 ("missing {0}: not the Stage 6.10A live tree (no startup demotion) - not starting" -f $f) }
}
foreach ($f in @($Python, $EnvFile, $Overrides, $DbPath)) { if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { Finish 1 "missing $f" } }
$sha = "unknown"
try { $sha = (& git -C $Live rev-parse --short HEAD 2>$null) } catch { }
Log ("1 live path OK: {0} (deployed {1})" -f $Live, $sha)

# ---- 2. paper environment --------------------------------------------------------------------------------------------------------------
$broker = (Resolve-Setting "EXECUTION_BROKER" "alpaca_paper").Trim().ToLower()
if ($broker -ne "alpaca_paper") { Finish 1 ("EXECUTION_BROKER resolves to '{0}', not alpaca_paper" -f $broker) }
$brokerPy = Get-Content -LiteralPath (Join-Path $Live "execution\broker.py") -Raw
if ($brokerPy -notmatch '(?m)^PAPER_HOST = "paper-api\.alpaca\.markets"\s*$' -or $brokerPy -notmatch '(?m)^BASE = f"https://\{PAPER_HOST\}"') {
    Finish 1 "execution\broker.py does not pin the Alpaca PAPER host"
}
foreach ($k in @("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_SECRET_KEY")) { if (-not (Resolve-Setting $k "")) { Finish 1 ("{0} is not set: the paper account cannot be proven" -f $k) } }
Log "2 paper environment OK: EXECUTION_BROKER=alpaca_paper, host pinned to paper-api.alpaca.markets in code, paper credentials set"

# ---- 3. config -------------------------------------------------------------------------------------------------------------------------
$policy = (Resolve-Setting "PAPER_AUTO_RUN_POLICY" "MANUAL_ARM").Trim().ToUpper()
$sched = Resolve-Setting "PAPER_AUTO_SCHEDULED_ARM_ENABLED" "false"
$saEnv = (Resolve-Setting "STOCK_AGENT_ENV" "").Trim().ToLower()
if ($policy -ne "MANUAL_ARM") { Finish 1 ("PAPER_AUTO_RUN_POLICY resolves to '{0}', not MANUAL_ARM" -f $policy) }
if (Truthy $sched) { Finish 1 "PAPER_AUTO_SCHEDULED_ARM_ENABLED resolves to true: scheduled Paper Auto must stay off" }
if (@("test", "browser_test") -contains $saEnv) { Finish 1 ("STOCK_AGENT_ENV={0} would keep the executor off" -f $saEnv) }
Log ("3 config OK: PAPER_AUTO_RUN_POLICY=MANUAL_ARM, PAPER_AUTO_SCHEDULED_ARM_ENABLED=false | AUTORUN_SUPERVISOR_ENABLED={0} EXECUTION_SCHEDULER_ENABLED={1} UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED={2}" -f `
    (Resolve-Setting "AUTORUN_SUPERVISOR_ENABLED" "true"), (Resolve-Setting "EXECUTION_SCHEDULER_ENABLED" "true"), (Resolve-Setting "UNIFIED_EXTERNAL_EXPOSURE_GATE_ENABLED" "false"))

# ---- 4. duplicate guard ----------------------------------------------------------------------------------------------------------------
. (Join-Path $Live "scripts\stock_agent_common.ps1")
Set-StrictMode -Off
$paths = Get-SAPaths $Live "" ""
$pidInfo = Read-SAPidFile $paths.Pid
$verified = ($null -ne $pidInfo) -and (Test-SAProcess $pidInfo)
$runners = @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine -like "*run_stock_agent.py*" -and $_.CommandLine -like ("*--app-root `"" + $Live + "`"*") })
$listen = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
$foreignPort = $null
foreach ($c in $listen) { if (@($runners | Where-Object { $_.ProcessId -eq $c.OwningProcess }).Count -eq 0) { $foreignPort = $c.OwningProcess } }
if ($verified -or $runners.Count -gt 0) {
    Log ("4 ALREADY RUNNING: PID file {0} (verified {1}); live runner processes [{2}]; port {3} listener(s) [{4}] - not starting a second instance" -f `
        $(if ($pidInfo) { $pidInfo.pid } else { "none" }), $verified, (($runners | ForEach-Object { $_.ProcessId }) -join ","), $Port, (($listen | ForEach-Object { $_.OwningProcess }) -join ","))
    $state = Read-State
    Log ("6 running instance: " + (Describe $state))
    Finish 0 ("already running; nothing started, nothing changed (mode {0})" -f $(if ($state) { $state.mode.mode } else { "unknown" }))
}
if ($foreignPort) {
    $n = (Get-Process -Id $foreignPort -ErrorAction SilentlyContinue).ProcessName
    Finish 1 ("port {0} is held by PID {1} ({2}), which is not the live Stock Agent - not starting" -f $Port, $foreignPort, $n)
}
Log ("4 no live instance running (port {0} free)" -f $Port)

if ($DryRun) { Finish 0 ("dry run: every check passed; would start the live server on port {0} through scripts\start_stock_agent.ps1" -f $Port) }

# ---- 5. start (canonical script; detached; Monitor only) -------------------------------------------------------------------------------
$startScript = Join-Path $Live "scripts\start_stock_agent.ps1"
Log "5 starting through scripts\start_stock_agent.ps1"
$out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript -AppRoot $Live -Python $Python -Port $Port -EnvFile $EnvFile -OverridesFile $Overrides -DbPath $DbPath 2>&1
$code = $LASTEXITCODE
foreach ($l in @($out)) { if ("$l".Trim()) { Log ("  start_stock_agent: " + "$l".Trim()) } }
if ($code -eq 3) {
    $state = Read-State
    Log ("6 running instance: " + (Describe $state))
    Finish 0 "another start won the race; already running, nothing changed"
}
if ($code -ne 0) { Finish 2 ("start_stock_agent.ps1 failed with exit code {0}" -f $code) }

# ---- 6. read back ----------------------------------------------------------------------------------------------------------------------
$state = Read-State
Log ("6 started instance: " + (Describe $state))
if (-not $state) { Finish 3 "started, but the state could not be read back" }
if ($state.mode.mode -ne "MONITOR_ONLY") { Finish 3 ("UNEXPECTED mode {0} after start (this script changes nothing; check the dashboard)" -f $state.mode.mode) }
if (-not $state.auto_run -or $state.auto_run.policy -ne "MANUAL_ARM" -or $state.auto_run.scheduled_arm_enabled) { Finish 3 "UNEXPECTED auto-run policy after start" }
Finish 0 "started in MONITOR_ONLY; Paper Auto OFF until a person arms it"
