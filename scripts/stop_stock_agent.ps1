<#
.SYNOPSIS
  Stop the Stock Agent server started by scripts\start_stock_agent.ps1 - and only that process.

.DESCRIPTION
  Reads runtime\stock_agent.pid, verifies that the PID is alive AND is our runner (its command line carries run_stock_agent.py and
  the instance id written at start), then asks it to stop (runtime\stock_agent.stop). The server shuts down gracefully: the setup
  evaluator and the paper executor threads stop through the normal app lifespan. Only if it does not exit within -TimeoutSeconds is
  that same verified PID terminated. No other python.exe is ever touched. Stopping never cancels or sends an order.
#>
[CmdletBinding()]
param(
    [string]$AppRoot = "",
    [string]$RuntimeDir = "",
    [string]$LogDir = "",
    [int]$TimeoutSeconds = 60
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "stock_agent_common.ps1")
$AppRoot = Resolve-SAAppRoot $AppRoot
$paths = Get-SAPaths $AppRoot $RuntimeDir $LogDir

$info = Read-SAPidFile $paths.Pid
if ($null -eq $info) { Write-Output "Stock Agent: NOT RUNNING (no PID file)"; exit 0 }
if (-not (Test-SAProcess $info)) {
    Remove-Item -LiteralPath $paths.Pid -Force -ErrorAction SilentlyContinue
    Write-Output ("Stock Agent: NOT RUNNING (stale PID file for PID {0} removed; no process was touched)" -f $info.pid)
    Write-SARunnerLog $paths.Runner ("stop: stale PID file removed (pid {0})" -f $info.pid)
    exit 0
}
$pidToStop = [int]$info.pid
Set-Content -LiteralPath $paths.Stop -Value $info.instance_id -Encoding utf8
Write-SARunnerLog $paths.Runner ("stop requested for pid {0} (instance {1})" -f $pidToStop, $info.instance_id)
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline -and (Test-SAProcess $info)) { Start-Sleep -Milliseconds 500 }
if (-not (Test-SAProcess $info)) {
    Start-Sleep -Milliseconds 500
    Remove-Item -LiteralPath $paths.Pid -Force -ErrorAction SilentlyContinue
    Write-Output ("Stock Agent: STOPPED cleanly (PID {0})" -f $pidToStop)
    Write-SARunnerLog $paths.Runner ("stopped cleanly (pid {0})" -f $pidToStop)
    exit 0
}
if (Test-SAProcess $info) {                                    # re-verified identity right before terminating
    Stop-Process -Id $pidToStop -Force -ErrorAction SilentlyContinue
    foreach ($p in @(Get-SARunnerProcesses $info.instance_id)) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
}
Start-Sleep -Milliseconds 500
Remove-Item -LiteralPath $paths.Pid -Force -ErrorAction SilentlyContinue
Write-Output ("Stock Agent: STOPPED (PID {0} did not exit within {1} s and was terminated; the next start reconciles before any write)" -f $pidToStop, $TimeoutSeconds)
Write-SARunnerLog $paths.Runner ("terminated after timeout (pid {0})" -f $pidToStop)
exit 0
