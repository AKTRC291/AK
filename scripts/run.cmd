@echo off
rem Pull latest macro lines/config, run a monitor job, then push alerts/ so the
rem Claude app relay can deliver them to your phone.
cd /d "%~dp0.."
rem Commits need an author; set one for this folder only if none is configured.
git config user.email >nul 2>&1 || git config user.email "psx-monitor@users.noreply.github.com"
git config user.name  >nul 2>&1 || git config user.name  "psx-monitor"
git pull -q --rebase >nul 2>&1
py -3.13 -m psx_monitor %*
if not exist alerts goto :eof
git add alerts
git diff --cached --quiet && goto :eof
git commit -q -m "alerts %1 %date% %time%"
git pull -q --rebase
git push -q || echo [run.cmd] git push FAILED - alerts not relayed to the Claude app
