@echo off
rem Pull the latest macro lines/config from GitHub, then run a monitor job.
cd /d "%~dp0.."
git pull -q --ff-only >nul 2>&1
py -3.13 -m psx_monitor %*
