@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\prepare-sadtalker-dev.ps1" %*
exit /b %errorlevel%
