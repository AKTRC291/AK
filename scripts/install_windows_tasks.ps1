# Registers PSX monitor jobs in Windows Task Scheduler (run in PowerShell from the repo folder).
# Times are your PC's local time - set Windows to (UTC+05:00) Islamabad, Karachi.
$repo = (Get-Location).Path
$run  = Join-Path $repo "scripts\run.cmd"   # git pull (fresh macro lines) + py -3.13 -m psx_monitor
function Add-Job($name, $mode, $trigger) {
  $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$run`" $mode" -WorkingDirectory $repo
  $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
  Register-ScheduledTask -TaskName "PSX-$name" -Action $action -Trigger $trigger -Settings $set -Force | Out-Null
  Write-Host "registered PSX-$name"
}
$days = 'Monday','Tuesday','Wednesday','Thursday','Friday'
Add-Job "PreOpen" "preopen" (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 9:15am)
Add-Job "Close"   "close"   (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 3:50pm)
# intraday: every 30 min 9:45-15:15 (script itself ignores times outside market hours)
$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 9:45am
$t.Repetition = (New-ScheduledTaskTrigger -Once -At 9:45am -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Hours 5 -Minutes 31)).Repetition
Add-Job "Intraday" "intraday" $t
