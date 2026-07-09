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
echo  [2] fish-audio-sdk (cloud voice-cloning TTS)
echo  [3] ffmpeg (video compositing — install manually)
echo  [4] CosyVoice (local voice cloning via embedded Python)
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
goto :fish

:: ===== 2. Fish Audio SDK (bundled, nothing to do) ===================
:fish
echo [2/4] fish-audio-sdk ...
echo   [OK] fish-audio-sdk is bundled in the package
echo.
goto :ffmpeg

:: ===== 3. ffmpeg ====================================================
:ffmpeg
echo [3/4] ffmpeg ...
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

:: ===== 4. CosyVoice (embedded Python sidecar) =======================
:cosyvoice
echo [4/4] CosyVoice (local voice cloning)...

:: --- Skip if already set up -----------------------------------------
if exist "%COSYVOICE_DIR%python.exe" (
    echo   [OK] Embedded Python already set up
    goto :cosyvoice_model
)

:: --- Find the embeddable Python zip bundled in _internal\packaging\ --
set "EMBED_ZIP=%INTERNAL_DIR%packaging\python-embed-win64.zip"
set "GET_PIP=%INTERNAL_DIR%packaging\get-pip.py"

if not exist "%EMBED_ZIP%" (
    echo   [FAIL] Embeddable Python zip not found at:
    echo          %EMBED_ZIP%
    echo   CosyVoice setup cannot continue.
    echo.
    goto :cosyvoice_end
)

:: --- Extract embeddable Python --------------------------------------
echo   Extracting embedded Python...
if not exist "%COSYVOICE_DIR%" mkdir "%COSYVOICE_DIR%"
powershell -Command "Expand-Archive -Path '%EMBED_ZIP%' -DestinationPath '%COSYVOICE_DIR%' -Force"
if not exist "%COSYVOICE_DIR%python.exe" (
    echo   [FAIL] Failed to extract embeddable Python
    goto :cosyvoice_end
)
echo   [OK] Python extracted

:: --- Remove python*._pth to enable site-packages + pip ---------------
:: The embeddable zip ships a version-named file (e.g. python311._pth), NOT
:: python._pth — a wildcard is required, otherwise site-packages stays
:: disabled and `python -m pip` fails with "No module named pip".
if exist "%COSYVOICE_DIR%python*._pth" (
    del "%COSYVOICE_DIR%python*._pth"
    echo   [OK] python*._pth removed (enables pip)
)

:: --- Install pip ----------------------------------------------------
echo   Installing pip...
"%COSYVOICE_DIR%python.exe" "%GET_PIP%" --quiet
if %errorlevel% neq 0 (
    echo   [FAIL] pip install failed
    goto :cosyvoice_end
)
echo   [OK] pip installed

:: --- Install PyTorch (CPU) + CosyVoice ------------------------------
echo   Installing PyTorch (CPU) + CosyVoice (this may take a few minutes)...
"%COSYVOICE_DIR%python.exe" -m pip install torch torchvision torchaudio ^
    --index-url https://download.pytorch.org/whl/cpu --quiet
if %errorlevel% neq 0 (
    echo   [FAIL] PyTorch install failed
    goto :cosyvoice_end
)
echo   [OK] PyTorch installed

"%COSYVOICE_DIR%python.exe" -m pip install cosyvoice --quiet
if %errorlevel% neq 0 (
    echo   [FAIL] CosyVoice install failed
    goto :cosyvoice_end
)
echo   [OK] CosyVoice installed

:: --- Download model -------------------------------------------------
:cosyvoice_model
echo   Checking CosyVoice model...
set "MODEL_DIR=%BUNDLE_DIR%pretrained_models\CosyVoice2-0.5B"
if exist "%MODEL_DIR%\model.pt" (
    echo   [OK] Model already downloaded
    goto :cosyvoice_done
)

echo   Downloading CosyVoice2-0.5B model (~1.5 GB, first time only)...
echo   This may take a while depending on your internet speed...
if not exist "%MODEL_DIR%" mkdir "%MODEL_DIR%"

:: Try using huggingface-cli first, fall back to direct download
"%COSYVOICE_DIR%python.exe" -m pip install "huggingface_hub[cli]" --quiet
"%COSYVOICE_DIR%python.exe" -m huggingface_hub.cli download ^
    FunAudioLLM/CosyVoice2-0.5B --local-dir "%MODEL_DIR%"
if %errorlevel% neq 0 (
    echo   [FAIL] Model download failed
    echo.
    echo   You can download manually:
    echo   huggingface-cli download FunAudioLLM/CosyVoice2-0.5B
    echo   --local-dir pretrained_models/CosyVoice2-0.5B
    goto :cosyvoice_end
)
echo   [OK] Model downloaded

:cosyvoice_done
echo.
echo   [OK] CosyVoice is ready to use!
echo   Start Media Agent, then set TTS Provider to "cosyvoice" in Settings.
echo.
goto :cosyvoice_end

:cosyvoice_end
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
