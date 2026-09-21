#!/usr/bin/env bash
# Manage the llm-dashboard LaunchAgent (24/7 background service).
#
#   ./service.sh install     render + load the agent, start it now
#   ./service.sh uninstall   stop it and remove the agent
#   ./service.sh restart     reload after a code change
#   ./service.sh status      is it running, what PID, last exit code
#   ./service.sh logs        tail the service logs
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.armandbriere.llm-dashboard"
TEMPLATE="$DIR/$LABEL.plist.template"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LOG_DIR="$HOME/Library/Logs/llm-dashboard"

# Everything here is user-scoped: a LaunchAgent lives in the gui/<uid> domain,
# which only exists for a logged-in user. Under sudo the uid is 0, gui/0 is not
# a real domain, and launchctl reports that as an opaque "Domain does not
# support specified action" (125). Refuse up front instead.
if [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: do not run this with sudo — $LABEL is a per-user LaunchAgent." >&2
    echo "       Run it as ${SUDO_USER:-your own user}, without sudo." >&2
    exit 1
fi

install_agent() {
    mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
    sed -e "s|__APP_DIR__|$DIR|g" -e "s|__HOME__|$HOME|g" "$TEMPLATE" > "$TARGET"
    plutil -lint "$TARGET" >/dev/null
    # bootout first so install doubles as an upgrade path
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$TARGET"
    launchctl enable "$DOMAIN/$LABEL"
    launchctl kickstart -k "$DOMAIN/$LABEL"
    echo "Installed and started $LABEL"
    echo "Dashboard: http://127.0.0.1:8000"
}

case "${1:-status}" in
    install)   install_agent ;;
    uninstall) launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
               rm -f "$TARGET"
               echo "Removed $LABEL" ;;
    restart)   # Nothing to kickstart if the agent was booted out (./service.sh
               # uninstall, make stop, a failed install). Bootstrap it instead
               # of failing with launchctl's "Could not find service" (113).
               if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
                   launchctl kickstart -k "$DOMAIN/$LABEL"
                   echo "Restarted $LABEL"
               else
                   echo "$LABEL is not loaded — installing it"
                   install_agent
               fi ;;
    status)    launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
                 | grep -E '^\s+(state|pid|last exit code|program) ' || echo "$LABEL is not loaded" ;;
    logs)      tail -f "$LOG_DIR/stdout.log" "$LOG_DIR/stderr.log" ;;
    *)         sed -n '2,9p' "$0"; exit 1 ;;
esac
