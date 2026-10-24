<#
.SYNOPSIS
  Run the Stage 6.0 one-share AAPL Alpaca PAPER smoke (scripts\run_aapl_paper_smoke.py) without VS Code or a terminal.

.DESCRIPTION
  Selects the project's Python, sets the working directory, passes the .env file BY PATH (the Python runner reads only the four
  Alpaca names from it) and runs the smoke. The smoke itself enforces everything: 09:45-10:00 ET on -RunDate only, the normal
  Stock Agent server must answer and be in Monitor only, fresh pre-flight checks, AAPL / 1 share / BUY then SELL / at most 2 orders,
  Alpaca PAPER only, the real Stage 6.0 executor submits every order, Paper Auto always off at the end, no second run.
  This wrapper never sends an order itself.

  Exit codes (from the smoke): 0 PASS, 1 FAIL, 2 NO ORDER (refused before any order), 3 REFUSED (duplicate / already ran).
  Output: logs\aapl_paper_smoke.log, runtime\aapl_paper_smoke_result.json, logs\aapl_paper_smoke_console*.log.

.EXAMPLE
  .\scripts\run_aapl_paper_smoke.ps1 -RunDate 2026-10-23
.EXAMPLE
  .\scripts\run_aapl_paper_smoke.ps1 -RunDate 2026-10-23 -Simulated -ClockEt "2026-10-23 09:46" -ServerUrl http://127.0.0.1:8031
#>
[CmdletBinding()]
param(
    [string]$AppRoot = "",
    [string]$Python = "",
    [string]$EnvFile = "",
    [string]$RuntimeDir = "",
    [string]$LogDir = "",
    [string]$RunDate = "",
    [string]$ServerUrl = "http://127.0.0.1:8000",
    [switch]$Simulated,
    [string]$ClockEt = ""
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "stock_agent_common.ps1")

$AppRoot = Resolve-SAAppRoot $AppRoot
$paths = Get-SAPaths $AppRoot $RuntimeDir $LogDir
New-Item -ItemType Directory -Force -Path $paths.Runtime, $paths.Logs | Out-Null
try { $py = Resolve-SAPython $AppRoot $Python } catch { Write-Output "AAPL paper smoke: NO ORDER - $($_.Exception.Message)"; exit 2 }
$envFileUsed = Resolve-SAEnvFile $AppRoot $EnvFile
if (-not $envFileUsed -and -not $Simulated) { Write-Output "AAPL paper smoke: NO ORDER - no .env file found (pass -EnvFile)"; exit 2 }
if (-not $RunDate) { $RunDate = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, "Eastern Standard Time").ToString("yyyy-MM-dd") }
if ($ClockEt -and -not $Simulated) { Write-Output "AAPL paper smoke: NO ORDER - -ClockEt is only allowed with -Simulated"; exit 2 }

$script = Join-Path $AppRoot "scripts\run_aapl_paper_smoke.py"
$argList = @("`"$script`"", "--app-root", "`"$AppRoot`"", "--runtime-dir", "`"$($paths.Runtime)`"", "--log-dir", "`"$($paths.Logs)`"", "--run-date", $RunDate, "--server-url", $ServerUrl)
if ($envFileUsed) { $argList += @("--env-file", "`"$envFileUsed`"") }
if ($Simulated) { $argList += "--simulated" }
if ($ClockEt) { $argList += @("--clock-et", "`"$ClockEt`"") }
$out = Join-Path $paths.Logs "aapl_paper_smoke_console.log"
$err = Join-Path $paths.Logs "aapl_paper_smoke_console_err.log"
Write-SARunnerLog $paths.Runner ("AAPL paper smoke wrapper: run date {0}, {1}, server {2}, python {3}" -f $RunDate, $(if ($Simulated) { "SIMULATED" } else { "ALPACA PAPER" }), $ServerUrl, $py)
$p = Start-Process -FilePath $py -ArgumentList ($argList -join " ") -WorkingDirectory $AppRoot -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput $out -RedirectStandardError $err
$code = $p.ExitCode
$label = @{ 0 = "PASS"; 1 = "FAIL"; 2 = "NO ORDER"; 3 = "REFUSED" }[[int]$code]
if (-not $label) { $label = "FAIL (exit $code)" }
$reason = ""
$resultFile = Join-Path $paths.Runtime "aapl_paper_smoke_result.json"
if (($code -eq 0 -or $code -eq 1 -or $code -eq 2) -and (Test-Path -LiteralPath $resultFile)) {
    try { $r = Get-Content -LiteralPath $resultFile -Raw | ConvertFrom-Json; if ($r.failure_reason) { $reason = " - " + $r.failure_reason } } catch { }
}
if ($code -eq 3) { $reason = " - another smoke run is active or already finished for $RunDate (see logs\aapl_paper_smoke.log)" }
Write-Output ("AAPL paper smoke: {0}{1}" -f $label, $reason)
Write-Output ("Log: {0}" -f (Join-Path $paths.Logs "aapl_paper_smoke.log"))
Write-Output ("Result: {0}" -f $resultFile)
Write-SARunnerLog $paths.Runner ("AAPL paper smoke wrapper: exit {0} ({1})" -f $code, $label)
exit $code
