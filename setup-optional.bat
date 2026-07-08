@echo off
title Media Agent - 可选依赖安装

echo ============================================
echo  Media Agent - 可选依赖安装
echo ============================================
echo.
echo 本脚本自动检测并安装缺失的可选组件。
echo.
echo   [1] Playwright + Chromium（用于 JS 渲染抓取）
echo   [2] fish-audio-sdk（云端声音克隆 TTS）
echo   [3] ffmpeg（视频合成 — 必须自己装）
echo   [4] CosyVoice（本地声音复刻 — 需 GPU）
echo.
echo ============================================
echo.

:: ===== Detect script dir ============================================
set "SCRIPT_DIR=%~dp0"
set "BUNDLE_DIR=%SCRIPT_DIR%"
set "INTERNAL_DIR=%BUNDLE_DIR%_internal\"

:: ===== 1. Playwright + Chromium =====================================
:playwright
echo [1/4] Playwright + Chromium ...

:: --- Check if playwright library is bundled (in _internal/) ----------
if exist "%INTERNAL_DIR%playwright\" (
    echo   [√] Playwright 已打包
) else (
    echo   [×] Playwright 未打包，请解压完整压缩包（含 _internal 目录）
    goto :chromium_skip
)

:: --- Check if Chromium browser is already installed ------------------
set "PW_CHROMIUM_DIR="
if exist "%USERPROFILE%\AppData\Local\ms-playwright\" (
    for /d %%d in ("%USERPROFILE%\AppData\Local\ms-playwright\chromium-*") do (
        if exist "%%d\chrome.exe" set "PW_CHROMIUM_DIR=%%d"
    )
)
if defined PW_CHROMIUM_DIR (
    echo   [√] Chromium 已安装
    goto :fish
)

:: --- Attempt Chromium install via bundled exe (v0.2.3+ only) ---------
echo   正在下载 Chromium 浏览器（约 300MB，仅首次需要）...
if exist "%BUNDLE_DIR%media-agent.exe" (
    "%BUNDLE_DIR%media-agent.exe" --run-module playwright install chromium
) else (
    echo   [×] 未找到 media-agent.exe
    goto :chromium_manual
)

:: Verify install succeeded
set "PW_CHROMIUM_DIR="
if exist "%USERPROFILE%\AppData\Local\ms-playwright\" (
    for /d %%d in ("%USERPROFILE%\AppData\Local\ms-playwright\chromium-*") do (
        if exist "%%d\chrome.exe" set "PW_CHROMIUM_DIR=%%d"
    )
)
if defined PW_CHROMIUM_DIR (
    echo   [√] Chromium 安装完成
) else (
:chromium_manual
    echo   [×] Chromium 自动安装失败
    echo.
    echo   可手动安装：
    echo     1. 确保已安装 Python 3.11+
    echo     2. pip install playwright
    echo     3. python -m playwright install chromium
    echo.
)
:chromium_skip
echo.
goto :fish

:: ===== 2. Fish Audio SDK（已内置）===================================
:fish
echo [2/4] fish-audio-sdk ...
echo   [√] fish-audio-sdk 已打包进主程序，无需额外安装
echo.
goto :ffmpeg

:: ===== 3. ffmpeg ====================================================
:ffmpeg
echo [3/4] ffmpeg ...
where ffmpeg >nul 2>nul
if %errorlevel% equ 0 (
    for /f "delims=" %%i in ('where ffmpeg') do set "FFPATH=%%i"
    echo   [√] ffmpeg 已安装: %FFPATH%
) else (
    echo   [×] ffmpeg 未找到
    echo.
    echo   请访问 https://ffmpeg.org/download.html 下载，
    echo   将 bin\ffmpeg.exe 所在目录添加到系统 PATH。
    echo.
    echo   或直接下载便携版到本目录：
    echo     curl -L https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip -o ffmpeg.zip
    echo     解压后把 ffmpeg.exe 放到 _internal\ 同目录
)
echo.
goto :cosyvoice

:: ===== 4. CosyVoice（本地声音复刻）==================================
:cosyvoice
echo [4/4] CosyVoice（本地声音复刻，可选）...
echo.
echo   CosyVoice 需要 Python ^< 3.13 + NVIDIA GPU + 显存 4GB+
echo   无法打包进单文件，需另外安装：
echo.
echo   1. 安装依赖:
echo      pip install cosyvoice
echo.
echo   2. 下载模型:
echo      huggingface-cli download FunAudioLLM/CosyVoice2-0.5B --local-dir pretrained_models/CosyVoice2-0.5B
echo.
echo   3. 启动 Media Agent，在“设置”页把 TTS Provider 切换为 cosyvoice
echo ============================================
echo  安装完成！
echo  如有问题请提交 Issue: https://github.com/ivanzwb/media-agent/issues
echo ============================================
pause
