#!/usr/bin/env bash
# Foreground launcher for a laptop session: creates the venv on first run,
# starts the server, and opens the dashboard in a browser.
#
#   ./start.sh              start and open the browser
#   ./start.sh --no-open    start only
#
# Configuration is read from the environment; see README.md "Configuration".
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if ! command -v uv >/dev/null 2>&1; then
    echo "error: 'uv' is required (https://docs.astral.sh/uv/). On macOS: brew install uv" >&2
    exit 1
fi

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
POLL="${LLM_DASHBOARD_POLL_SECONDS:-300}"

echo "==> Syncing the virtual environment"
uv sync --no-dev --quiet

echo "==> Starting LLM Quota Tracker on http://${HOST}:${PORT}"
echo "    Quota is polled every ${POLL}s (LLM_DASHBOARD_POLL_SECONDS)."

# Open the browser on an interactive macOS session.
if [[ "$OSTYPE" == "darwin"* ]] && [ -z "${CI:-}" ] && [ "${1:-}" != "--no-open" ]; then
    (sleep 1.5 && open "http://${HOST}:${PORT}") &
fi

exec .venv/bin/python -m uvicorn backend.main:app --host "$HOST" --port "$PORT"
