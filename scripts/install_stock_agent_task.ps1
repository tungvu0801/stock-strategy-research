<#
.SYNOPSIS
  Register (or print, or remove) a Windows Task Scheduler task that STARTS the Stock Agent server. It never sends an order.

.DESCRIPTION
  The task runs scripts\start_stock_agent.ps1 -Foreground as the current user, only while that user is logged on (no admin
  rights needed). The start script supervises the server and restarts it after a crash at most 3 times; a second copy of the
  task is ignored (MultipleInstances IgnoreNew) and the start script itself refuses a duplicate process. The server decides on
  its own whether it may trade (Monitor only unless Paper Auto was enabled; startup reconciliation first; Alpaca PAPER only).

  Triggers:
    -Trigger Logon      at user logon (recommended for continuous use)
    -Trigger Weekdays   Monday-Friday at -EasternTime (New York time, converted to this PC's local time)
    -Trigger Once       one start on -Date at -EasternTime (e.g. a supervised smoke: 09:35 ET)

  -PrintOnly prints the task definition without registering anything. -Unregister removes the task named -TaskName.

.EXAMPLE
  .\scripts\install_stock_agent_task.ps1 -Trigger Logon -PrintOnly
.EXAMPLE
  .\scripts\install_stock_agent_task.ps1 -Trigger Once -Date 2026-10-23 -EasternTime 09:35 -TaskName StockAgentSmokeStart
.EXAMPLE
  .\scripts\install_stock_agent_task.ps1 -TaskName StockAgentSmokeStart -Unregister
#>
[CmdletBinding()]
param(
    [ValidateSet("Logon", "Weekdays", "Once")][string]$Trigger = "Logon",
    [string]$EasternTime = "09:20",
    [string]$Date = "",
    [string]$TaskName = "StockAgent",
    [string]$AppRoot = "",
    [string]$Python = "",
    [string]$EnvFile = "",
    [int]$Port = 8000,
    [string]$DbPath = "",
    [string]$RuntimeDir = "",
    [string]$LogDir = "",
    [switch]$PrintOnly,
    [switch]$Unregister
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "stock_agent_common.ps1")

if ($TaskName -notmatch '^[A-Za-z0-9_.-]+$') { throw "TaskName may contain letters, digits, '_', '.', '-' only" }
if ($Unregister) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $t) { Write-Output "Task '$TaskName' is not registered."; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Task '$TaskName' removed. (A running Stock Agent keeps running; stop it with scripts\stop_stock_agent.ps1.)"
    exit 0
}

$AppRoot = Resolve-SAAppRoot $AppRoot
$py = Resolve-SAPython $AppRoot $Python
$envFileUsed = Resolve-SAEnvFile $AppRoot $EnvFile
$start = Join-Path $AppRoot "scripts\start_stock_agent.ps1"
$argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", "`"$start`"", "-Foreground", "-AppRoot", "`"$AppRoot`"", "-Python", "`"$py`"", "-Port", $Port)
if ($envFileUsed) { $argList += @("-EnvFile", "`"$envFileUsed`"") }
if ($DbPath) { $argList += @("-DbPath", "`"$DbPath`"") }
if ($RuntimeDir) { $argList += @("-RuntimeDir", "`"$RuntimeDir`"") }
if ($LogDir) { $argList += @("-LogDir", "`"$LogDir`"") }
$arguments = $argList -join " "

function ConvertFrom-Eastern([datetime]$EasternWallClock) {
    $et = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    return [TimeZoneInfo]::ConvertTime([datetime]::SpecifyKind($EasternWallClock, [DateTimeKind]::Unspecified), $et, [TimeZoneInfo]::Local)
}

$user = "$env:USERDOMAIN\$env:USERNAME"
switch ($Trigger) {
    "Logon" {
        $trig = New-ScheduledTaskTrigger -AtLogOn -User $user
        $when = "at logon of $user"
    }
    "Weekdays" {
        $today = (Get-Date).Date
        $local = ConvertFrom-Eastern ([datetime]::ParseExact(("{0} {1}" -f $today.ToString("yyyy-MM-dd"), $EasternTime), "yyyy-MM-dd HH:mm", $null))
        $trig = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $local
        $when = "Monday-Friday at {0} ET = {1} local" -f $EasternTime, $local.ToString("HH:mm")
    }
    "Once" {
        if (-not $Date) { throw "-Trigger Once needs -Date yyyy-MM-dd" }
        $local = ConvertFrom-Eastern ([datetime]::ParseExact(("{0} {1}" -f $Date, $EasternTime), "yyyy-MM-dd HH:mm", $null))
        if ($local -lt (Get-Date)) { throw ("{0} {1} ET ({2} local) is in the past" -f $Date, $EasternTime, $local.ToString("yyyy-MM-dd HH:mm")) }
        $trig = New-ScheduledTaskTrigger -Once -At $local
        $when = "once on {0} at {1} ET = {2} local" -f $Date, $EasternTime, $local.ToString("yyyy-MM-dd HH:mm")
    }
}
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $AppRoot
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$desc = "Starts the Stock Agent server (Alpaca PAPER only; it never sends an order by itself starting). Stop with scripts\stop_stock_agent.ps1."
$task = New-ScheduledTask -Action $action -Trigger $trig -Settings $settings -Principal $principal -Description $desc

Write-Output ("Task name: {0}" -f $TaskName)
Write-Output ("Trigger: {0}" -f $when)
Write-Output ("Runs as: {0} (only when logged on, no elevation)" -f $user)
Write-Output ("Execute: powershell.exe")
Write-Output ("Arguments: {0}" -f $arguments)
Write-Output ("Settings: MultipleInstances={0} RestartCount={1} RestartInterval={2} ExecutionTimeLimit={3} StartWhenAvailable={4} WakeToRun={5}" -f `
    $settings.MultipleInstances, $settings.RestartCount, $settings.RestartInterval, $settings.ExecutionTimeLimit, $settings.StartWhenAvailable, $settings.WakeToRun)
if ($PrintOnly) { Write-Output "PrintOnly: nothing registered."; exit 0 }
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
$reg = Get-ScheduledTask -TaskName $TaskName
Write-Output ("Registered: {0} (state {1}). Remove with: .\scripts\install_stock_agent_task.ps1 -TaskName {0} -Unregister" -f $TaskName, $reg.State)
exit 0
