@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\prepare-cosyvoice-dev.ps1" %*
exit /b %errorlevel%
