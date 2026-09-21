#!/usr/bin/env bash
# Service-mode launcher for launchd. Unlike start.sh it never opens a browser
# and never blocks on a TTY. Called by the LaunchAgent.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
LOG_DIR="${LLM_DASHBOARD_LOG_DIR:-$HOME/Library/Logs/llm-dashboard}"
MAX_LOG_BYTES=$((5 * 1024 * 1024))

mkdir -p "$LOG_DIR"

# launchd does not rotate its StandardOut/ErrorPath files. Roll them at boot
# so a service running for months does not fill the disk.
for f in "$LOG_DIR/stdout.log" "$LOG_DIR/stderr.log"; do
    if [ -f "$f" ] && [ "$(stat -f%z "$f")" -gt "$MAX_LOG_BYTES" ]; then
        mv -f "$f" "$f.1"
    fi
done

# Bootstrap the venv if it is missing (e.g. fresh clone). `make deploy` keeps
# it in sync afterwards; doing it here on every restart would need the network.
if [ ! -x ".venv/bin/python" ]; then
    uv sync --no-dev --quiet
fi

CMD=(.venv/bin/python -m uvicorn backend.main:app --host "$HOST" --port "$PORT")

# Opt-in: hold a power assertion so the Mac does not idle-sleep and stop
# polling. Has no effect when the lid is closed without external power+display.
if [ "${LLM_DASHBOARD_CAFFEINATE:-0}" = "1" ]; then
    exec /usr/bin/caffeinate -is "${CMD[@]}"
fi

exec "${CMD[@]}"
