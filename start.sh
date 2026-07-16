#!/bin/bash
set -e

echo "=== Media Agent — 一键启动 ==="
echo ""

# ── 1. Check Python ──
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
    echo "[ERROR] Python not found. Install Python 3.11+ from https://python.org"
    exit 1
fi
PY=$(command -v python3 || command -v python)
$PY -c "import sys; exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null || {
    echo "[ERROR] Need Python 3.11+. Current:"
    $PY --version
    exit 1
}
echo "[OK] Python ($($PY --version 2>&1))"

# ── 2. Check Node.js ──
if ! command -v node &>/dev/null; then
    echo "[ERROR] Node.js not found. Install from https://nodejs.org"
    exit 1
fi
echo "[OK] Node.js ($(node --version))"

# ── 3. Install Python deps ──
# On Windows (Git Bash / MINGW), the venv activation path differs.
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    VENV_ACTIVATE=".venv/Scripts/activate"
    VENV_PY=".venv/Scripts/python.exe"
    ;;
  *)
    VENV_ACTIVATE=".venv/bin/activate"
    VENV_PY=".venv/bin/python"
    ;;
esac

if [ -d .venv ]; then
    echo "[SKIP] Python venv already exists"
else
    echo "[..] Creating Python venv..."
    $PY -m venv .venv
fi
# shellcheck disable=SC1090
source "$VENV_ACTIVATE"
# Re-capture PY now that venv is active so later calls use venv Python
PY=$(command -v python3 || command -v python)
pip install -q -r requirements.txt 2>/dev/null
echo "[OK] Python dependencies"

# ── 4. Build React SPA (always rebuild) ──
# On Windows Git Bash the bundled `npm` bash shim prints a harmless
# "line 14: [: =: unary operator expected" (its `uname` test); use npm.cmd
# there to bypass the shim entirely.
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) NPM="npm.cmd" ;;
  *)                    NPM="npm" ;;
esac
echo "[..] Building frontend..."
cd frontend
"$NPM" install --silent
"$NPM" run build
cd ..
echo "[OK] Frontend built"

# ── 5. Init data dir ──
if [ ! -d data ]; then
    $PY -m app.cli init >/dev/null
    echo "[OK] Data directory initialized"
fi

# ── 6. Start server ──
echo ""
echo "=== Starting server at http://127.0.0.1:8000 ==="
echo ""
$PY -m app.cli serve
