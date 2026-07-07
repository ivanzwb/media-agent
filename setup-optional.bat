@echo off
chcp 65001 >nul
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

:: ── Detect script dir ──────────────────────────
set "SCRIPT_DIR=%~dp0"
set "BUNDLE_DIR=%SCRIPT_DIR%media-agent"

:: ── 1. playwright + chromium ───────────────────
:playwright
echo [1/4] Playwright + Chromium ...
if not exist "%BUNDLE_DIR%" (
    echo [WARN] 没找到 media-agent 目录，请把本脚本放 dist/ 下运行。
    goto :eof
)
"%BUNDLE_DIR%\media-agent.exe" -c "import playwright; print('ok')" 2>nul
if %errorlevel% equ 0 (
    echo   [✓] Playwright 库已打包
) else (
    echo   [✗] Playwright 库缺失，需重建打包
)
echo   正在下载 Chromium 浏览器（约 300MB，首次只需一次）...
"%BUNDLE_DIR%\media-agent.exe" -c "from playwright.sync_api import sync_playwright; sync_playwright().start(); print('Chromium installed')" 2>nul
if %errorlevel% equ 0 (
    echo   [✓] Chromium 安装完成
) else (
    echo   [✗] Chromium 安装失败，尝试用 playwright CLI 安装...
    "%BUNDLE_DIR%\media-agent.exe" -m playwright install chromium 2>nul
)
echo.
goto :fish

:: ── 2. fish-audio-sdk ──────────────────────────
:fish
echo [2/4] fish-audio-sdk ...
echo   [✓] fish-audio-sdk 已打包进主程序，无需额外安装
echo.
goto :ffmpeg

:: ── 3. ffmpeg ──────────────────────────────────
:ffmpeg
echo [3/4] ffmpeg ...
where ffmpeg >nul 2>nul
if %errorlevel% equ 0 (
    for /f "delims=" %%i in ('where ffmpeg') do set "FFPATH=%%i"
    echo   [✓] ffmpeg 已安装: %FFPATH%
) else (
    echo   [✗] ffmpeg 未找到
    echo.
    echo   请从 https://ffmpeg.org/download.html 下载，
    echo   解压后把 bin\ffmpeg.exe 所在目录加到系统 PATH。
    echo.
    echo   或直接下载便携版放在本目录：
    echo     curl -L https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip -o ffmpeg.zip
    echo     解压后把 ffmpeg.exe 放在 media-agent\_internal\ 同目录
)
echo.
goto :cosyvoice

:: ── 4. CosyVoice ───────────────────────────────
:cosyvoice
echo [4/4] CosyVoice（本地声音复刻，可选）...
echo.
echo   CosyVoice 需要 Python ^< 3.13 + NVIDIA GPU + 显存 4GB+
echo   无法打包进单文件，需另外安装：
echo.
echo   1. 创建独立 Python 环境（Python 3.11）：
echo      conda create -n cosyvoice python=3.11
echo      conda activate cosyvoice
echo.
echo   2. 安装依赖：
echo      pip install "setuptools<70" cosyvoice
echo.
echo   3. 启动 CosyVoice HTTP 服务：
echo      python -m app.tts.providers.cosyvoice_http --port 8888
echo.
echo   4. 在 Media Agent 设置页配置：
echo      TTS Provider: cosyvoice
echo      API Base: http://127.0.0.1:8888
echo.
echo ============================================
echo  安装完成！
echo  如有问题请提交 Issue: https://github.com/ivanzwb/media-agent/issues
echo ============================================
pause
