<#
.SYNOPSIS
  Register (or print, or remove) the one-time Task Scheduler task "StockAgentSmokeRun" that launches scripts\run_aapl_paper_smoke.ps1.

.DESCRIPTION
  The task only launches project code. The smoke decides everything itself (09:45-10:00 ET window on -Date, server in Monitor only,
  fresh pre-flight, AAPL / 1 share / BUY then SELL / at most 2 orders via the real Stage 6.0 executor, Alpaca PAPER only, Paper Auto
  always off at the end, never a second run). Task settings: runs as you, only while logged on, no elevation; one start, no repetition,
  NO restart on failure (a retry could mean another trade), a second instance is ignored, not started late after a missed time
  (StartWhenAvailable off), no wake-to-run, stopped after 45 minutes.

  Before registering it reports this PC's sleep timeout and how to keep the PC awake (it never changes power settings itself).

.EXAMPLE
  .\scripts\install_aapl_smoke_task.ps1 -Date 2026-10-23 -PrintOnly
.EXAMPLE
  .\scripts\install_aapl_smoke_task.ps1 -Date 2026-10-23
.EXAMPLE
  .\scripts\install_aapl_smoke_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$Date = "",
    [string]$EasternTime = "09:45",
    [string]$TaskName = "StockAgentSmokeRun",
    [string]$AppRoot = "",
    [string]$Python = "",
    [string]$EnvFile = "",
    [string]$RuntimeDir = "",
    [string]$LogDir = "",
    [string]$ServerUrl = "http://127.0.0.1:8000",
    [switch]$PrintOnly,
    [switch]$Unregister
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "stock_agent_common.ps1")
if ($TaskName -notmatch '^[A-Za-z0-9_.-]+$') { throw "TaskName may contain letters, digits, '_', '.', '-' only" }
if ($Unregister) {
    if ($null -eq (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) { Write-Output "Task '$TaskName' is not registered."; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Task '$TaskName' removed."
    exit 0
}
if (-not $Date) { throw "-Date yyyy-MM-dd is required (the New York date of the smoke)" }

# ---- power report (read only) --------------------------------------------------------------------------------------------------------
function Get-AcSleepSeconds {
    $q = (powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE) -join "`n"
    if ($q -match 'Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)') { return [Convert]::ToInt32($Matches[1], 16) }
    return -1
}
$acSleep = Get-AcSleepSeconds
if ($acSleep -eq 0) { Write-Output "PC sleep (plugged in): NEVER - fine for the smoke window." }
elseif ($acSleep -gt 0) {
    $mins = [Math]::Round($acSleep / 60)
    Write-Output ("WARNING: this PC sleeps after {0} minute(s) idle when plugged in. A sleeping PC runs neither the bot nor the smoke (wake-to-run is off)." -f $mins)
    Write-Output "  Optional, run it yourself before the smoke:  powercfg /change standby-timeout-ac 0"
    Write-Output ("  Restore afterwards:                          powercfg /change standby-timeout-ac {0}" -f $mins)
} else { Write-Output "PC sleep (plugged in): could not be read (powercfg)." }

$AppRoot = Resolve-SAAppRoot $AppRoot
$py = Resolve-SAPython $AppRoot $Python
$envFileUsed = Resolve-SAEnvFile $AppRoot $EnvFile
if (-not $envFileUsed) { throw "no .env file found (pass -EnvFile)" }
$wrapper = Join-Path $AppRoot "scripts\run_aapl_paper_smoke.ps1"
$argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", "`"$wrapper`"", "-AppRoot", "`"$AppRoot`"", "-Python", "`"$py`"",
             "-EnvFile", "`"$envFileUsed`"", "-RunDate", $Date, "-ServerUrl", $ServerUrl)
if ($RuntimeDir) { $argList += @("-RuntimeDir", "`"$RuntimeDir`"") }
if ($LogDir) { $argList += @("-LogDir", "`"$LogDir`"") }
$arguments = $argList -join " "

$et = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$local = [TimeZoneInfo]::ConvertTime([datetime]::SpecifyKind([datetime]::ParseExact(("{0} {1}" -f $Date, $EasternTime), "yyyy-MM-dd HH:mm", $null), [DateTimeKind]::Unspecified), $et, [TimeZoneInfo]::Local)
if ($local -lt (Get-Date)) { throw ("{0} {1} ET ({2} local) is in the past" -f $Date, $EasternTime, $local.ToString("yyyy-MM-dd HH:mm")) }
$user = "$env:USERDOMAIN\$env:USERNAME"
$trig = New-ScheduledTaskTrigger -Once -At $local
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $AppRoot
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 45) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trig -Settings $settings -Principal $principal -Description "Stage 6.0 one-share AAPL Alpaca PAPER smoke (launches project code only; the smoke enforces its own window and limits)."

Write-Output ("Task name: {0}" -f $TaskName)
Write-Output ("Trigger: once on {0} at {1} ET = {2} local" -f $Date, $EasternTime, $local.ToString("yyyy-MM-dd HH:mm"))
Write-Output ("Runs as: {0} (only when logged on, no elevation)" -f $user)
Write-Output "Execute: powershell.exe"
Write-Output ("Arguments: {0}" -f $arguments)
Write-Output ("Settings: MultipleInstances={0} RestartCount={1} StartWhenAvailable={2} WakeToRun={3} ExecutionTimeLimit={4}" -f `
    $settings.MultipleInstances, $settings.RestartCount, $settings.StartWhenAvailable, $settings.WakeToRun, $settings.ExecutionTimeLimit)
if ($PrintOnly) { Write-Output "PrintOnly: nothing registered."; exit 0 }
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Output ("Registered: {0} (next run {1}). Remove with: .\scripts\install_aapl_smoke_task.ps1 -TaskName {0} -Unregister" -f $TaskName, $info.NextRunTime)
exit 0
