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
    restart)   launchctl kickstart -k "$DOMAIN/$LABEL"; echo "Restarted $LABEL" ;;
    status)    launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
                 | grep -E '^\s+(state|pid|last exit code|program) ' || echo "$LABEL is not loaded" ;;
    logs)      tail -f "$LOG_DIR/stdout.log" "$LOG_DIR/stderr.log" ;;
    *)         sed -n '2,9p' "$0"; exit 1 ;;
esac
