@echo off
title Media Agent - Optional Component Setup

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo  Media Agent - Optional Component Setup
echo ============================================
echo.
echo  This script auto-detects and installs missing optional components.
echo.
echo  [1] Playwright + Chromium (for JS-rendered page scraping)
echo  [2] ffmpeg (video compositing — install manually)
echo  [3] CosyVoice (local voice cloning via embedded Python)
echo  [4] SadTalker (digital-human presenter lip-sync — optional, GPU)
echo.
echo ============================================
echo.

set "BUNDLE_DIR=%~dp0"
set "INTERNAL_DIR=%BUNDLE_DIR%_internal\"
set "COSYVOICE_DIR=%BUNDLE_DIR%_cosyvoice-python\"

:: ===== 1. Playwright + Chromium =====================================
:playwright
echo [1/4] Playwright + Chromium ...

:: --- Check if playwright library is bundled (in _internal/) ----------
if exist "%INTERNAL_DIR%playwright\" (
    echo   [OK] Playwright library is bundled
) else (
    echo   [SKIP] Playwright library not found — re-extract the full archive
    goto :chromium_skip
)

:: --- Check if a Chromium browser is already installed ----------------
:: The app scrapes with headless Chromium, which uses the ~115 MB headless
:: shell — so we only need chromium-headless-shell, not the full ~185 MB
:: Chromium. Accept either if already present (headless shell OR full).
call :detect_chromium
if defined PW_CHROMIUM_DIR (
    echo   [OK] Chromium already installed
    goto :fish
)

:: --- Attempt install via bundled exe (headless shell only) -----------
echo   Downloading Chromium headless shell (~115 MB, first time only)...
:: Raise Playwright's download timeout (default 30s) so a slow connection
:: doesn't abort and re-download from scratch.
set "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=180000"
if exist "%BUNDLE_DIR%media-agent.exe" (
    "%BUNDLE_DIR%media-agent.exe" --run-module playwright install chromium-headless-shell
) else (
    echo   [SKIP] media-agent.exe not found
    goto :chromium_manual
)

:: Verify install succeeded
call :detect_chromium
if defined PW_CHROMIUM_DIR (
    echo   [OK] Chromium installed
) else (
:chromium_manual
    echo   [FAIL] Chromium auto-install failed
    echo.
    echo   Manual install:
    echo     1. Make sure Python 3.11+ is installed
    echo     2. pip install playwright
    echo     3. python -m playwright install chromium-headless-shell
    echo.
)
:chromium_skip
echo.
goto :ffmpeg

:: ===== 2. ffmpeg ====================================================
:ffmpeg
echo [2/4] ffmpeg ...
where ffmpeg >nul 2>nul
if %errorlevel% equ 0 (
    for /f "delims=" %%i in ('where ffmpeg') do set "FFPATH=%%i"
    echo   [OK] ffmpeg found: %FFPATH%
) else (
    echo   [SKIP] ffmpeg not found
    echo.
    echo   Download from https://ffmpeg.org/download.html
    echo   Add bin\ffmpeg.exe to your system PATH, or
    echo   place ffmpeg.exe next to _internal\.
    echo.
)
echo.
goto :cosyvoice

:: ===== 3. CosyVoice (prebuilt runtime — NO compilation) =============
:cosyvoice
echo [3/4] CosyVoice (local voice cloning — prebuilt runtime)...

set "RUNTIME_DIR=%BUNDLE_DIR%_cosyvoice-runtime"
set "RUNTIME_PY=%RUNTIME_DIR%\python.exe"

:: --- Skip if the prebuilt runtime is already extracted --------------
if exist "%RUNTIME_PY%" (
    echo   [OK] CosyVoice runtime already installed
    goto :cosyvoice_model
)

:: --- Download the prebuilt, self-contained runtime -------------------
:: This is a conda-pack'd environment with torch/torchaudio/onnxruntime +
:: pynini/openfst ALL prebuilt — nothing is compiled on this machine.
set "RUNTIME_TGZ=%BUNDLE_DIR%cosyvoice-runtime-win64.tar.gz"
if not exist "%RUNTIME_TGZ%" (
    echo   Downloading prebuilt CosyVoice runtime ^(~1GB, first time only^)...
    powershell -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://github.com/ivanzwb/release/releases/latest/download/cosyvoice-runtime-win64.tar.gz' -OutFile '%RUNTIME_TGZ%'"
    if %errorlevel% neq 0 (
        echo   [FAIL] Failed to download CosyVoice runtime
        goto :cosyvoice_end
    )
)

:: --- Extract (tar ships with Windows 10+) ---------------------------
echo   Extracting runtime...
if not exist "%RUNTIME_DIR%" mkdir "%RUNTIME_DIR%"
tar -xzf "%RUNTIME_TGZ%" -C "%RUNTIME_DIR%"
if not exist "%RUNTIME_PY%" (
    echo   [FAIL] Failed to extract runtime
    goto :cosyvoice_end
)

:: --- Finalize conda-pack relocation (rewrites absolute paths) --------
echo   Finalizing runtime...
if exist "%RUNTIME_DIR%\Scripts\conda-unpack.exe" (
    "%RUNTIME_DIR%\Scripts\conda-unpack.exe"
) else (
    "%RUNTIME_PY%" -m conda_pack.scripts.conda_unpack 2>nul
)
del "%RUNTIME_TGZ%" 2>nul
echo   [OK] CosyVoice runtime ready: %RUNTIME_DIR%

:: --- Download model -------------------------------------------------
:cosyvoice_model
echo   Checking CosyVoice model...
set "RUNTIME_PY=%BUNDLE_DIR%_cosyvoice-runtime\python.exe"
set "MODEL_DIR=%BUNDLE_DIR%pretrained_models\CosyVoice2-0.5B"
if exist "%MODEL_DIR%\model.pt" (
    echo   [OK] Model already downloaded
    goto :cosyvoice_done
)

echo   Downloading CosyVoice2-0.5B model (~1.5 GB, first time only)...
echo   This may take a while depending on your internet speed...
if not exist "%MODEL_DIR%" mkdir "%MODEL_DIR%"

:: huggingface_hub ships inside the prebuilt runtime, so no install needed.
set "HF_ENDPOINT=https://hf-mirror.com"
:: Disable the Xet backend — its CAS server (cas-server.xethub.hf.co) is not
:: served by the mirror and returns 401, breaking the download. Falling back
:: to classic HTTP downloads works with hf-mirror.com.
set "HF_HUB_DISABLE_XET=1"
set "HF_XET_DISABLE=1"
:: Longer per-request timeout to survive slow TLS handshakes to the mirror.
set "HF_HUB_DOWNLOAD_TIMEOUT=60"
:: Retry loop — snapshot_download resumes partial files, so re-running
:: continues from where a timeout/drop left off (large model, flaky mirror).
set _dl_tries=0
:cosyvoice_dl
set /a _dl_tries+=1
"%RUNTIME_PY%" -c ^
"from huggingface_hub import snapshot_download; snapshot_download('FunAudioLLM/CosyVoice2-0.5B', local_dir=r'%MODEL_DIR%', max_workers=2)"
if %errorlevel% equ 0 goto :cosyvoice_dl_ok
if %_dl_tries% lss 8 (
    echo   下载中断，第 %_dl_tries%/8 次重试（断点续传）...
    timeout /t 3 >nul
    goto :cosyvoice_dl
)
echo   [FAIL] Model download failed
echo.
echo   You can re-run this script to resume, or download manually:
echo   huggingface-cli download FunAudioLLM/CosyVoice2-0.5B
echo   --local-dir pretrained_models/CosyVoice2-0.5B
goto :cosyvoice_end
:cosyvoice_dl_ok
echo   [OK] Model downloaded

:cosyvoice_done
echo.
echo   [OK] CosyVoice is ready to use!
echo   Start Media Agent, then set TTS Provider to "cosyvoice" in Settings.
echo.
goto :cosyvoice_end

:cosyvoice_end
echo.
goto :sadtalker

:: ===== 4. SadTalker (digital-human presenter lip-sync, optional) ====
:sadtalker
echo [4/4] SadTalker (digital-human lip-sync, optional)...
set "SADTALKER_DIR=%BUNDLE_DIR%SadTalker"
if exist "%SADTALKER_DIR%\inference.py" if exist "%SADTALKER_DIR%\checkpoints\" (
    echo   [OK] SadTalker already set up: %SADTALKER_DIR%
    echo   In Settings, set the SadTalker dir to that path.
    goto :finish
)
echo   SadTalker enables lip-synced digital-human presenter. It is LARGE
echo   ^(~5GB models^) and needs an NVIDIA GPU. Without it the static-head
echo   fallback still works.
set /p "_ans=  Install SadTalker now? [y/N] "
if /i "!_ans!"=="y" goto :sadtalker_go
if /i "!_ans!"=="yes" goto :sadtalker_go
echo   [SKIP] Skipped SadTalker.
goto :finish

:sadtalker_go
where git >nul 2>nul
if %errorlevel% neq 0 (
    echo   [SKIP] git not found — install Git, then re-run.
    goto :finish
)
set "SPY=%BUNDLE_DIR%_cosyvoice-runtime\python.exe"
if not exist "%SPY%" set "SPY=python"
echo   Cloning SadTalker...
if not exist "%SADTALKER_DIR%\.git" git clone --depth 1 https://github.com/OpenTalker/SadTalker.git "%SADTALKER_DIR%"
if exist "%SADTALKER_DIR%\requirements.txt" (
    echo   Installing SadTalker requirements ^(GPU torch recommended^)...
    "%SPY%" -m pip install -r "%SADTALKER_DIR%\requirements.txt"
    if !errorlevel! neq 0 echo   [WARN] Some deps failed — install matching-CUDA torch per SadTalker README.
)
echo   Downloading SadTalker checkpoints ^(~5GB, first time only^)...
"%SPY%" -m pip install huggingface_hub --quiet
set "HF_ENDPOINT=https://hf-mirror.com"
set "HF_HUB_DISABLE_XET=1"
"%SPY%" -c "from huggingface_hub import snapshot_download; snapshot_download('vinthony/SadTalker', local_dir=r'%SADTALKER_DIR%\checkpoints')"
if exist "%SADTALKER_DIR%\checkpoints\" (
    echo   [OK] SadTalker ready: %SADTALKER_DIR%
    echo   In Settings -^> Video -^> Digital Human, set that dir and enable it.
) else (
    echo   [WARN] Checkpoints missing. See https://github.com/OpenTalker/SadTalker
)
goto :finish

:finish
echo.
echo ============================================
echo  Setup complete!
echo  Report issues: https://github.com/ivanzwb/media-agent/issues
echo ============================================
pause
exit /b 0

:: ===== Subroutines ==================================================
:: Detect an installed Chromium — headless shell (preferred, ~115 MB) or
:: the full Chromium — and set PW_CHROMIUM_DIR to its folder if found.
:detect_chromium
:: Playwright nests the executable one level down (chrome-win64\chrome.exe,
:: chrome-headless-shell-win64\chrome-headless-shell.exe) — check those, with
:: a flat-path fallback for older layouts.
set "PW_CHROMIUM_DIR="
if exist "%USERPROFILE%\AppData\Local\ms-playwright\" (
    for /d %%d in ("%USERPROFILE%\AppData\Local\ms-playwright\chromium_headless_shell-*") do (
        if exist "%%d\chrome-headless-shell-win64\chrome-headless-shell.exe" set "PW_CHROMIUM_DIR=%%d"
        if exist "%%d\chrome-headless-shell.exe" set "PW_CHROMIUM_DIR=%%d"
    )
    for /d %%d in ("%USERPROFILE%\AppData\Local\ms-playwright\chromium-*") do (
        if exist "%%d\chrome-win64\chrome.exe" set "PW_CHROMIUM_DIR=%%d"
        if exist "%%d\chrome.exe" set "PW_CHROMIUM_DIR=%%d"
    )
)
goto :eof
