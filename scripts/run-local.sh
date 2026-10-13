#!/usr/bin/env bash
# PaperCI on your own macOS or Linux machine: the same agent as https://paperci.vercel.app, your own LLM.
#   bash scripts/run-local.sh           # set up, check, start
#   bash scripts/run-local.sh --check   # set up and check only
# Needs Python 3.11+ and Docker (Docker Desktop on macOS). Then open http://127.0.0.1:8000
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then echo "== creating .venv"; python3 -m venv .venv; fi
echo "== installing requirements"
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp deploy/local/local.env.example .env
  echo "== created .env from deploy/local/local.env.example (Ollama preset). Edit it to pick your models."
fi

echo "== preflight"
export PYTHONIOENCODING=utf-8
.venv/bin/python -m paperci.localrun check --build
[ "${1:-}" = "--check" ] && exit 0

echo "== PaperCI is starting on http://127.0.0.1:8000 (Ctrl+C to stop)"
exec .venv/bin/python -u -m paperci.web
