#!/bin/bash
# Start the ACBC GivEnergy Dashboard (loads the launchd service) and open it.
APP_LABEL="com.acbcsoftware.givenergy"
PLIST="$HOME/Library/LaunchAgents/${APP_LABEL}.plist"
INSTALL_DIR="$HOME/Library/Application Support/ACBCGivEnergyDashboard"

launchctl load "$PLIST" 2>/dev/null || true

WEB_PORT=$(grep -E '^\s*web_port' "$INSTALL_DIR/config.ini" 2>/dev/null | head -1 | sed 's/.*=//' | tr -d ' ')
[ -z "$WEB_PORT" ] && WEB_PORT=7890

echo "Dashboard starting — opening http://localhost:${WEB_PORT}"
sleep 2
open "http://localhost:${WEB_PORT}"
