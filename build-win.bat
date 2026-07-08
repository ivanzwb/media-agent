@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo === Media Agent Windows Build ===

:: Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Install Python 3.11+ from https://python.org
    pause & exit /b 1
)
echo Python OK

:: Install deps
echo Installing dependencies...
pip install -r requirements.txt
pip install "click<8.2" pyinstaller fish-audio-sdk playwright
if %errorlevel% neq 0 (
    echo [ERROR] pip install failed
    pause & exit /b 1
)

:: Build
echo Building...
pyinstaller --onedir ^
    --collect-all "app" ^
    --name "media-agent" ^
    --add-data "app/web/templates;app/web/templates" ^
    --add-data "app/web/static;app/web/static" ^
    --add-data "app/licensing/public_key.b64;app/licensing" ^
    --exclude-module "torch" ^
    --exclude-module "torchvision" ^
    --exclude-module "torchaudio" ^
    --exclude-module "transformers" ^
    --exclude-module "scipy" ^
    --exclude-module "matplotlib" ^
    --exclude-module "pandas" ^
    --exclude-module "sklearn" ^
    --exclude-module "scikit-learn" ^
    --exclude-module "librosa" ^
    --exclude-module "numba" ^
    --exclude-module "soundfile" ^
    --exclude-module "onnxruntime" ^
    --exclude-module "tensorflow" ^
    --exclude-module "pyarrow" ^
    --exclude-module "IPython" ^
    --exclude-module "jedi" ^
    --exclude-module "parso" ^
    --exclude-module "pytest" ^
    --exclude-module "nbformat" ^
    --exclude-module "jsonschema" ^
    --exclude-module "lark" ^
    --exclude-module "modelscope" ^
    --exclude-module "lightning" ^
    --exclude-module "hydra" ^
    --exclude-module "altair" ^
    --hidden-import "uvicorn" ^
    --hidden-import "uvicorn.logging" ^
    --hidden-import "uvicorn.loops.auto" ^
    --hidden-import "uvicorn.protocols.http.auto" ^
    --hidden-import "uvicorn.protocols.websockets.auto" ^
    --hidden-import "fastapi" ^
    --hidden-import "jinja2" ^
    --hidden-import "cryptography" ^
    --hidden-import "cryptography.fernet" ^
    --hidden-import "apscheduler" ^
    --hidden-import "apscheduler.triggers.cron" ^
    --hidden-import "httpx" ^
    --console ^
    app/cli.py

if %errorlevel% neq 0 (
    echo [ERROR] Build failed
    pause & exit /b 1
)

rmdir /s /q build 2>nul
del media-agent.spec 2>nul
copy setup-optional.bat dist\media-agent\ >nul

:: Zip
echo Packaging...
powershell -Command "Compress-Archive -Path dist\media-agent\* -DestinationPath dist\media-agent-win64.zip -Force"

echo === Build complete ===
dir dist\media-agent-win64.zip
echo.
echo Run:  dist\media-agent\media-agent.exe serve
pause
