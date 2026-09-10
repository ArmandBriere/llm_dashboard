#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "===================================================="
echo "⚡ Starting LLM Quota Tracker (Claude Code Native)"
echo "===================================================="

# Ensure virtualenv exists
if [ ! -f ".venv/bin/python" ]; then
    echo "Creating virtual environment..."
    if command -v uv >/dev/null 2>&1; then
        uv venv .venv
        uv pip install -r requirements.txt --python .venv/bin/python
    else
        python3 -m venv .venv
        .venv/bin/pip install -r requirements.txt
    fi
fi

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

echo "Starting server on http://${HOST}:${PORT} ..."
echo "Background collector will poll Claude Code quota every 5 minutes."

# Open browser if on macOS and interactive
if [[ "$OSTYPE" == "darwin"* ]] && [ -z "$CI" ] && [ "$1" != "--no-open" ]; then
    (sleep 1.5 && open "http://${HOST}:${PORT}") &
fi

exec .venv/bin/python -m uvicorn backend.main:app --host "$HOST" --port "$PORT"
