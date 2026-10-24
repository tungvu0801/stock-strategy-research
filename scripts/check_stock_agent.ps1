<#
.SYNOPSIS
  Is the background Stock Agent running? Prints a short status read from the server's own read-only API.

.DESCRIPTION
  RUNNING / NOT RUNNING from runtime\stock_agent.pid (verified against the live process), then - when running - the server's
  reachability, the execution mode, the risk-controls state, the reconciliation status and whether the paper executor loop runs
  (GET /api/execution/status; no broker call). Exit codes: 0 running and reachable, 1 not running, 2 running but not reachable.
#>
[CmdletBinding()]
param(
    [string]$AppRoot = "",
    [string]$RuntimeDir = "",
    [string]$LogDir = ""
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "stock_agent_common.ps1")
$AppRoot = Resolve-SAAppRoot $AppRoot
$paths = Get-SAPaths $AppRoot $RuntimeDir $LogDir

$info = Read-SAPidFile $paths.Pid
if (-not (Test-SAProcess $info)) {
    Write-Output "Stock Agent: NOT RUNNING"
    if ($null -ne $info) { Write-Output ("(a stale PID file for PID {0} exists; the next start or stop removes it)" -f $info.pid) }
    exit 1
}
Write-Output "Stock Agent: RUNNING"
Write-Output ("PID: {0}   Port: {1}   Since: {2}" -f $info.pid, $info.port, $info.started_at)
try {
    $s = Invoke-SAGet ([int]$info.port) "/api/execution/status" 15
} catch {
    Write-Output ("Server: NOT reachable on http://127.0.0.1:{0} ({1})" -f $info.port, $_.Exception.Message)
    exit 2
}
Write-Output ("Server: reachable (http://127.0.0.1:{0})" -f $info.port)
$mode = $s.mode_label
if ($s.attention) { $mode = "$mode (needs attention)" }
Write-Output ("Mode: {0}" -f $mode)
if ($s.safety_label) { Write-Output ("Safety: {0}" -f $s.safety_label) } else { Write-Output "Safety: unavailable" }
if ($s.reconciliation_healthy) { Write-Output "Reconciliation: Healthy" } else { Write-Output ("Reconciliation: Needs attention - {0}" -f (($s.attention_reasons | Select-Object -First 3) -join "; ")) }
$loop = $s.scheduler
if ($null -ne $loop -and $loop.running) { Write-Output "Executor loop: running" } else { Write-Output "Executor loop: not running" }
Write-Output ("Open paper positions: {0}   Orders waiting: {1}" -f $s.open_positions, $s.pending_entries)
try {                                                          # Stage 6.0.1: the read-only account refresher
    $a = Invoke-SAGet ([int]$info.port) "/api/account-refresh" 15
    $age = if ($null -ne $a.age_seconds) { [int][Math]::Round([double]$a.age_seconds) } else { $null }
    switch ($a.status) {
        "FRESH" { Write-Output ("Account data: Fresh ({0}s ago)" -f $age) }
        "STALE" { Write-Output ("Account data: Stale - {0}" -f $a.reason) }
        "STARTING" { Write-Output "Account data: Checking (first refresh of this process pending)" }
        default { Write-Output ("Account data unavailable - {0}" -f $a.reason) }
    }
    if ($null -ne $a.loop -and $a.loop.running) { Write-Output "Account refresh loop: running" } else { Write-Output "Account refresh loop: not running" }
} catch { Write-Output "Account data: status unavailable" }
try {                                                          # Stage 6.0.2: the read-only cash-flow ledger (deposits / withdrawals)
    $c = Invoke-SAGet ([int]$info.port) "/api/cash-flow-status" 15
    $cage = if ($null -ne $c.age_seconds) { [int][Math]::Round([double]$c.age_seconds) } else { $null }
    switch ($c.status) {
        "FRESH" { Write-Output ("Cash-flow data: Fresh ({0}s ago)" -f $cage) }
        "STALE" { Write-Output ("Cash-flow data: Stale - {0}" -f $c.reason) }
        "AMBIGUOUS" { Write-Output ("Cash-flow data: Unclear - {0}" -f $c.reason) }
        default { Write-Output ("Cash-flow data: unavailable - {0}" -f $c.reason) }
    }
    if ($null -ne $c.adjusted -and $null -ne $c.adjusted.adjusted_daily_pnl) {
        Write-Output ("Today: trading P/L {0}   deposits/withdrawals {1}   daily loss {2}% of limit {3}%" -f $c.adjusted.adjusted_daily_pnl, $c.net_external_cash_flow, $c.adjusted.adjusted_daily_loss_pct, $c.adjusted.threshold_pct)
    }
} catch { Write-Output "Cash-flow data: status unavailable" }
exit 0
