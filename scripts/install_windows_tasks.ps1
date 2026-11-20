# Registers PSX monitor jobs in Windows Task Scheduler (run in PowerShell from the repo folder).
# Times are your PC's local time - set Windows to (UTC+05:00) Islamabad, Karachi.
# PSX hours: Mon-Thu 09:30-15:30, Fri 09:00-16:30.
$repo = (Get-Location).Path
$run  = Join-Path $repo "scripts\run.cmd"   # git pull (fresh macro lines) + job + push alerts
function Add-Job($name, $mode, $triggers) {
  $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$run`" $mode" -WorkingDirectory $repo
  $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
  Register-ScheduledTask -TaskName "PSX-$name" -Action $action -Trigger $triggers -Settings $set -Force | Out-Null
  Write-Host "registered PSX-$name"
}
function Repeat($day, $start, $hours, $mins) {
  $t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $day -At $start
  $t.Repetition = (New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Minutes 30) `
                   -RepetitionDuration (New-TimeSpan -Hours $hours -Minutes $mins)).Repetition
  return $t
}
$monThu = 'Monday','Tuesday','Wednesday','Thursday'
Add-Job "PreOpen" "preopen" @(
  (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $monThu -At 9:15am),
  (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday  -At 8:45am))
Add-Job "Close" "close" @(
  (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $monThu -At 3:50pm),
  (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday  -At 4:50pm))
# intraday every 30 min: Mon-Thu 09:45-15:15, Fri 09:15-16:15 (script ignores times outside market hours)
Add-Job "Intraday" "intraday" @(
  (Repeat $monThu "9:45am" 5 31),
  (Repeat 'Friday' "9:15am" 7 1))
