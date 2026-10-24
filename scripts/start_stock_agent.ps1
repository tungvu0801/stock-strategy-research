<#
.SYNOPSIS
  Start the Stock Agent server in the background (no VS Code, no terminal window needed).

.DESCRIPTION
  Runs scripts\run_stock_agent.py with the project's Python environment. That process serves the normal dashboard
  (the same app as `uvicorn api.server:app`), including the Stage 5.8 setup evaluator and the Stage 6.0 paper executor loop.
  The executor itself decides whether it may trade (Monitor only by default; Paper Auto only after an explicit user choice,
  behind its startup reconciliation and the Stage 5.9.1 safety gate, Alpaca PAPER only). Starting the process never sends an order.

  Background (default): detached process (created through WMI, so closing VS Code or this window does not stop it); waits until
  the server answers, then prints the PID and the URL.
  -Foreground: used by Windows Task Scheduler. Supervises the process and restarts it after a crash, at most -MaxRestarts times
  (-RestartDelaySeconds apart, default 60). A deliberate stop (scripts\stop_stock_agent.ps1) is never restarted.

  Exit codes: 0 started (or, with -Foreground, a clean stop / already running), 1 failed, 3 already running, 4 port in use.

.EXAMPLE
  .\scripts\start_stock_agent.ps1
.EXAMPLE
  .\scripts\start_stock_agent.ps1 -Port 8000 -EnvFile "C:\Data Analyst Py\DA IDX\stock-agent\.env"
#>
[CmdletBinding()]
param(
    [string]$AppRoot = "",
    [string]$Python = "",
    [string]$EnvFile = "",
    [int]$Port = 8000,
    [string]$RuntimeDir = "",
    [string]$LogDir = "",
    [string]$DbPath = "",
    [string]$OverridesFile = "",
    [switch]$LoopbackOnly,
    [switch]$Foreground,
    [int]$MaxRestarts = 3,
    [int]$RestartDelaySeconds = 60,
    [int]$StartupTimeoutSeconds = 120
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "stock_agent_common.ps1")

$AppRoot = Resolve-SAAppRoot $AppRoot
$paths = Get-SAPaths $AppRoot $RuntimeDir $LogDir
New-Item -ItemType Directory -Force -Path $paths.Runtime, $paths.Logs | Out-Null
try { $py = Resolve-SAPython $AppRoot $Python } catch { Write-Output "Stock Agent: START FAILED - $($_.Exception.Message)"; exit 1 }
$envFileUsed = Resolve-SAEnvFile $AppRoot $EnvFile
$launcher = Join-Path $AppRoot "scripts\run_stock_agent.py"
if (-not (Test-Path -LiteralPath $launcher)) { Write-Output "Stock Agent: START FAILED - $launcher not found"; exit 1 }

$script:staleNote = $null
function Test-AlreadyRunning {
    # returns the running instance's PID info, or $null; a stale PID file is removed (no process is touched)
    $info = Read-SAPidFile $paths.Pid
    if ($null -eq $info) { return $null }
    if (Test-SAProcess $info) { return $info }
    Remove-Item -LiteralPath $paths.Pid -Force -ErrorAction SilentlyContinue
    $script:staleNote = ("Stock Agent: removed a stale PID file (PID {0} is not a running Stock Agent runner; no process was touched)" -f $info.pid)
    Write-SARunnerLog $paths.Runner ("stale PID file removed (pid {0})" -f $info.pid)
    return $null
}

function Get-LaunchArgs([string]$InstanceId) {
    $a = @("`"$launcher`"", "--app-root", "`"$AppRoot`"", "--instance-id", $InstanceId, "--port", $Port, "--runtime-dir", "`"$($paths.Runtime)`"", "--log-dir", "`"$($paths.Logs)`"")
    if ($envFileUsed) { $a += @("--env-file", "`"$envFileUsed`"") }
    if ($OverridesFile) { $a += @("--overrides-file", "`"$((Resolve-Path -LiteralPath $OverridesFile).Path)`"") }
    if ($DbPath) { $a += @("--db-path", "`"$DbPath`"") }
    if ($LoopbackOnly) { $a += "--loopback-only" }
    return ($a -join " ")
}

function Test-PortBusy {
    $c = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($c.Count -eq 0) { return $null }
    $owner = $c[0].OwningProcess
    $name = (Get-Process -Id $owner -ErrorAction SilentlyContinue).ProcessName
    return ("PID {0} ({1})" -f $owner, $name)
}

$running = Test-AlreadyRunning
if ($script:staleNote) { Write-Output $script:staleNote }
if ($null -ne $running) {
    Write-Output ("Stock Agent: ALREADY RUNNING (PID {0}, port {1}) - not starting a second instance" -f $running.pid, $running.port)
    Write-SARunnerLog $paths.Runner ("duplicate start refused (running pid {0})" -f $running.pid)
    if ($Foreground) { exit 0 } else { exit 3 }
}
$busy = Test-PortBusy
if ($busy) { Write-Output ("Stock Agent: START FAILED - port {0} is already in use by {1}" -f $Port, $busy); exit 4 }
if (-not $envFileUsed) { Write-Output "Stock Agent: warning - no .env file found; the server starts without Alpaca credentials (pass -EnvFile)" }

# ------------------------------------------------------------------ foreground: Task Scheduler supervisor ------------------------------
if ($Foreground) {
    $attempt = 0
    while ($true) {
        Remove-Item -LiteralPath $paths.Stop -Force -ErrorAction SilentlyContinue
        $iid = [guid]::NewGuid().ToString("N")
        Write-SARunnerLog $paths.Runner ("foreground start (attempt {0}, instance {1}, python {2}, env file {3})" -f ($attempt + 1), $iid, $py, $(if ($envFileUsed) { $envFileUsed } else { "none" }))
        $p = Start-Process -FilePath $py -ArgumentList (Get-LaunchArgs $iid) -WorkingDirectory $AppRoot -WindowStyle Hidden -PassThru -Wait
        $code = $p.ExitCode
        $stopAsked = (Test-Path -LiteralPath $paths.Stop) -and ((Get-Content -LiteralPath $paths.Stop -Raw -ErrorAction SilentlyContinue) -like "*$iid*")
        Write-SARunnerLog $paths.Runner ("process ended with exit code {0}{1}" -f $code, $(if ($stopAsked) { " after a stop request" } else { "" }))
        if ($code -eq 0 -or $stopAsked -or $code -eq 3) { exit 0 }
        $attempt += 1
        if ($attempt -gt $MaxRestarts) { Write-SARunnerLog $paths.Runner ("giving up after {0} restarts" -f $MaxRestarts); exit 1 }
        Write-SARunnerLog $paths.Runner ("restarting in {2} s ({0} of {1}); the executor reconciles with the broker before any write" -f $attempt, $MaxRestarts, $RestartDelaySeconds)
        Start-Sleep -Seconds $RestartDelaySeconds
        if ($null -ne (Test-AlreadyRunning)) { exit 0 }
        if (Test-PortBusy) { Write-SARunnerLog $paths.Runner "port busy; not restarting"; exit 1 }
    }
}

# ------------------------------------------------------------------ background: detached process ------------------------------------------
Remove-Item -LiteralPath $paths.Stop -Force -ErrorAction SilentlyContinue
$iid = [guid]::NewGuid().ToString("N")
$cmd = "`"$py`" " + (Get-LaunchArgs $iid)
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
$res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd; CurrentDirectory = $AppRoot; ProcessStartupInformation = $startup }
if ($res.ReturnValue -ne 0) { Write-Output ("Stock Agent: START FAILED - Win32_Process.Create returned {0}" -f $res.ReturnValue); exit 1 }
$spawned = [int]$res.ProcessId
Write-SARunnerLog $paths.Runner ("background start: spawned PID {0}, instance {1}, python {2}, env file {3}, port {4}" -f $spawned, $iid, $py, $(if ($envFileUsed) { $envFileUsed } else { "none" }), $Port)

$deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
$ok = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 750
    $info = Read-SAPidFile $paths.Pid
    $alive = @(Get-SARunnerProcesses $iid).Count -gt 0
    if ($info -and $info.instance_id -eq $iid) {
        try { $h = Invoke-SAGet $Port "/api/health" 5; if ($h.status -eq "ok") { $ok = $true; break } } catch { }
    }
    if (-not $alive) { break }
}
if (-not $ok) {
    Write-Output "Stock Agent: START FAILED - the server did not come up. Last lines of the logs:"
    foreach ($f in @($paths.Stderr, $paths.Stdout)) { if (Test-Path -LiteralPath $f) { Write-Output "--- $f"; Get-Content -LiteralPath $f -Tail 15 } }
    $left = @(Get-SARunnerProcesses $iid)
    foreach ($p in $left) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-SARunnerLog $paths.Runner "background start failed"
    exit 1
}
$info = Read-SAPidFile $paths.Pid
Write-Output ("Stock Agent: STARTED (PID {0}) http://127.0.0.1:{1}" -f $info.pid, $Port)
Write-Output ("Logs: {0} | {1}" -f $paths.Stdout, $paths.Stderr)
Write-Output "Status: .\scripts\check_stock_agent.ps1    Stop: .\scripts\stop_stock_agent.ps1"
exit 0
