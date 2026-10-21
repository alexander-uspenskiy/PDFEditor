#!/usr/bin/env bash
# Starts the local PDF editor and find-and-replace service on 127.0.0.1.
# First run: creates .venv, installs Python packages, and builds the editor (needs Node.js).
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q -r requirements.txt

if [ ! -f static/app/index.html ]; then
  (cd frontend && npm install --no-audit --no-fund && npm run build)
fi

echo "PDF editor:          http://127.0.0.1:${PORT}/"
echo "API reference:       http://127.0.0.1:${PORT}/docs"
exec uvicorn main:app --host 127.0.0.1 --port "${PORT}"
