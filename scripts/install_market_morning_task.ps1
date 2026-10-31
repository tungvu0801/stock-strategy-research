<#
.SYNOPSIS
  Register (or print, or remove) the Windows Task Scheduler task "Stock Agent - Market Morning" that runs scripts\start_market_morning.ps1.
  The wrapper starts the LIVE Stock Agent server in Monitor only (Alpaca PAPER); it never sends an order and never arms Paper Auto.

.DESCRIPTION
  The task runs as the current user, only while that user is logged on (no admin rights needed), and fires on THREE triggers:
    * weekly, Monday-Friday at -EasternTime (New York time, converted to this PC's local time; 09:15 ET = 06:15 Pacific, the
      current live schedule);
    * at logon of the user (-NoLogonTrigger removes it);
    * on resume from sleep or hibernation: the System event log, provider Microsoft-Windows-Power-Troubleshooter, event id 1,
      after -ResumeDelayMinutes (-NoResumeTrigger removes it). A PC that slept through 06:15 starts the bot when it wakes instead
      of waiting for the next morning.
  The wrapper is idempotent (duplicate guard: an already running live server is only read, never started twice), so extra
  triggers never create a second instance. Settings: MultipleInstances IgnoreNew, restart -RestartCount times every
  -RestartIntervalMinutes when the wrapper fails, execution time limit -ExecutionTimeLimitMinutes, StartWhenAvailable (a missed
  start runs when the PC is back), WakeToRun (honoured only when the power plan allows wake timers). Registering the same
  -TaskName again replaces that task: it never creates a second one.

  -PrintOnly prints the task definition (triggers, settings, event subscription) and registers nothing. -Unregister removes -TaskName.
  Enable Task Scheduler history once (elevated) so every start, stop and time-out of the task is recorded:
      wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true

.EXAMPLE
  .\scripts\install_market_morning_task.ps1 -PrintOnly
.EXAMPLE
  .\scripts\install_market_morning_task.ps1
  (registers "Stock Agent - Market Morning" for <this tree>\scripts\start_market_morning.ps1)
.EXAMPLE
  .\scripts\install_market_morning_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$TaskName = "Stock Agent - Market Morning",
    [string]$AppRoot = "",
    [string]$WrapperPath = "",
    [string]$EasternTime = "09:15",
    [int]$ResumeDelayMinutes = 2,
    [int]$RestartCount = 3,
    [int]$RestartIntervalMinutes = 10,
    [int]$ExecutionTimeLimitMinutes = 30,
    [switch]$NoLogonTrigger,
    [switch]$NoResumeTrigger,
    [switch]$PrintOnly,
    [switch]$Unregister
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "stock_agent_common.ps1")

if ($TaskName -notmatch '^[A-Za-z0-9 _.-]+$') { throw "TaskName may contain letters, digits, spaces, '_', '.', '-' only" }
if ($Unregister) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $t) { Write-Output "Task '$TaskName' is not registered."; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Task '$TaskName' removed. (A running Stock Agent keeps running; stop it with scripts\stop_stock_agent.ps1.)"
    exit 0
}

$AppRoot = Resolve-SAAppRoot $AppRoot
if (-not $WrapperPath) { $WrapperPath = Join-Path $AppRoot "scripts\start_market_morning.ps1" }
if (-not $PrintOnly -and -not (Test-Path -LiteralPath $WrapperPath -PathType Leaf)) { throw "Wrapper not found: $WrapperPath" }
$arguments = ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $WrapperPath)

function ConvertFrom-Eastern([datetime]$EasternWallClock) {
    $et = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    return [TimeZoneInfo]::ConvertTime([datetime]::SpecifyKind($EasternWallClock, [DateTimeKind]::Unspecified), $et, [TimeZoneInfo]::Local)
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$today = (Get-Date).Date
$local = ConvertFrom-Eastern ([datetime]::ParseExact(("{0} {1}" -f $today.ToString("yyyy-MM-dd"), $EasternTime), "yyyy-MM-dd HH:mm", $null))
$triggers = @()
$triggers += New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $local
$when = @(("weekly Monday-Friday at {0} ET = {1} local" -f $EasternTime, $local.ToString("HH:mm")))
if (-not $NoLogonTrigger) {
    $triggers += New-ScheduledTaskTrigger -AtLogOn -User $user
    $when += ("at logon of {0}" -f $user)
}
$subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name=''Microsoft-Windows-Power-Troubleshooter''] and EventID=1]]</Select></Query></QueryList>'
$delay = ("PT{0}M" -f $ResumeDelayMinutes)
if (-not $NoResumeTrigger) {
    $eventClass = Get-CimClass -Namespace "Root/Microsoft/Windows/TaskScheduler" -ClassName MSFT_TaskEventTrigger
    $resume = New-CimInstance -CimClass $eventClass -ClientOnly
    $resume.Enabled = $true
    $resume.Subscription = $subscription
    $resume.Delay = $delay
    $triggers += $resume
    $when += ("on resume from sleep (System log, Microsoft-Windows-Power-Troubleshooter event 1, delay {0})" -f $delay)
}
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $AppRoot
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount $RestartCount -RestartInterval (New-TimeSpan -Minutes $RestartIntervalMinutes) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $ExecutionTimeLimitMinutes) -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$desc = "Starts the live Stock Agent server in Monitor only through scripts\start_market_morning.ps1 (Alpaca PAPER only; GET requests only; it never arms Paper Auto or sends an order). Stop the server with scripts\stop_stock_agent.ps1."
$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description $desc

Write-Output ("Task name: {0}" -f $TaskName)
Write-Output ("Wrapper: {0}" -f $WrapperPath)
Write-Output ("Triggers: {0}" -f ($when -join "; "))
Write-Output ("Runs as: {0} (only when logged on, no elevation)" -f $user)
Write-Output ("Execute: powershell.exe")
Write-Output ("Arguments: {0}" -f $arguments)
Write-Output ("Settings: MultipleInstances={0} RestartCount={1} RestartInterval={2} ExecutionTimeLimit={3} StartWhenAvailable={4} WakeToRun={5}" -f `
    $settings.MultipleInstances, $settings.RestartCount, $settings.RestartInterval, $settings.ExecutionTimeLimit, $settings.StartWhenAvailable, $settings.WakeToRun)
if (-not $NoResumeTrigger) { Write-Output ("Event subscription: {0}" -f $subscription) }
Write-Output "Task history: enable once, elevated: wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true"
if ($PrintOnly) { Write-Output "PrintOnly: nothing registered."; exit 0 }
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
$reg = Get-ScheduledTask -TaskName $TaskName
Write-Output ("Registered: {0} (state {1}). Remove with: .\scripts\install_market_morning_task.ps1 -TaskName '{0}' -Unregister" -f $TaskName, $reg.State)
exit 0
