#!/bin/bash
# Stop the ACBC GivEnergy Dashboard (unloads the launchd service).
APP_LABEL="com.acbcsoftware.givenergy"
PLIST="$HOME/Library/LaunchAgents/${APP_LABEL}.plist"

launchctl unload "$PLIST" 2>/dev/null || true
echo "Dashboard stopped. Use start-dashboard.command (or log in again) to restart it."
sleep 1
