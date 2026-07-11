@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo === Media Agent — 一键启动 ===
echo.

:: ── 1. Check Python ──
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Install Python 3.11+ from https://python.org
    pause & exit /b 1
)
python -c "import sys; exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Need Python 3.11+. Current:
    python --version
    pause & exit /b 1
)
echo [OK] Python

:: ── 2. Check Node.js ──
where node >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Node.js not found. Install from https://nodejs.org
    pause & exit /b 1
)
echo [OK] Node.js

:: ── 3. Install Python deps (skip if already done) ──
if exist .venv (
    echo [SKIP] Python venv already exists
) else (
    echo [..] Creating Python venv...
    python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt 2>nul
echo [OK] Python dependencies

:: ── 4. Build React SPA ──
if exist frontend\dist\index.html (
    echo [SKIP] Frontend already built — delete frontend\dist\ to rebuild
) else (
    echo [..] Building frontend...
    cd frontend
    call npm install --silent
    call npm run build
    cd ..
    echo [OK] Frontend built
)

:: ── 5. Init data dir ──
if not exist data (
    python -m app.cli init >nul
    echo [OK] Data directory initialized
)

:: ── 6. Start server ──
echo.
echo === Starting server at http://127.0.0.1:8000 ===
echo.
call python -m app.cli serve

pause
