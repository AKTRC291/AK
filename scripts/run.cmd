@echo off
rem Pull latest macro lines/config, run a monitor job, then push alerts/ so the
rem Claude app relay can deliver them to your phone.
cd /d "%~dp0.."
git pull -q --rebase >nul 2>&1
py -3.13 -m psx_monitor %*
if exist alerts (
  git add alerts >nul 2>&1
  git diff --cached --quiet || (
    git commit -q -m "alerts %1 %date% %time%" >nul 2>&1
    git pull -q --rebase >nul 2>&1
    git push -q >nul 2>&1
  )
)
