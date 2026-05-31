#!/usr/bin/env bash
# ============================================================
#  ACBC GivEnergy Dashboard — Linux / Raspberry Pi UPDATER
#  Upgrades an existing install to a newer version WITHOUT
#  touching your settings (config.ini) or history (history.db).
#
#  Run from inside the freshly-downloaded project folder:
#       bash update.sh
# ============================================================
set -euo pipefail

RUN_USER="$(whoami)"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Sanity check: must run from the new download folder ────────────────────────
[ -f "dashboard_server.py" ] || error "Run this from the downloaded project folder (dashboard_server.py not found here)."

# ── Auto-detect the installed service + directory ──────────────────────────────
# Works regardless of where it was installed (handles both the standard
# /opt install and custom locations) by reading systemd's own records.
SERVICE=""
INSTALL_DIR=""
for svc in givenergy-dashboard givenergy; do
    wd="$(systemctl show -p WorkingDirectory --value "$svc" 2>/dev/null || true)"
    if [ -n "$wd" ] && [ -d "$wd" ]; then
        SERVICE="$svc"; INSTALL_DIR="$wd"; break
    fi
done
[ -n "$SERVICE" ]          || error "No givenergy service found. Use setup.sh for a fresh install."
[ -d "$INSTALL_DIR/venv" ] || error "No virtual environment at $INSTALL_DIR. Use setup.sh instead."

info "Found service '$SERVICE' installed at $INSTALL_DIR"

# ── 1. Stop the service ────────────────────────────────────────────────────────
info "Stopping the dashboard service..."
sudo systemctl stop "$SERVICE" 2>/dev/null || warn "Service was not running."

# ── 2. Back up settings + database (safety net) ───────────────────────────────
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$INSTALL_DIR/backups/$STAMP"
sudo mkdir -p "$BACKUP"
[ -f "$INSTALL_DIR/config.ini" ] && sudo cp "$INSTALL_DIR/config.ini" "$BACKUP/"
[ -f "$INSTALL_DIR/history.db" ] && sudo cp "$INSTALL_DIR/history.db" "$BACKUP/"
# Also grab the WAL/SHM sidecar files if present
[ -f "$INSTALL_DIR/history.db-wal" ] && sudo cp "$INSTALL_DIR/history.db-wal" "$BACKUP/" 2>/dev/null || true
[ -f "$INSTALL_DIR/history.db-shm" ] && sudo cp "$INSTALL_DIR/history.db-shm" "$BACKUP/" 2>/dev/null || true
info "Backed up config.ini + history.db to $BACKUP"

# ── 3. Copy ONLY the application files ─────────────────────────────────────────
#  config.ini and history.db are deliberately NOT in this list — they are
#  never overwritten, so your settings and history are kept intact.
APP_FILES="dashboard_server.py dashboard.html manifest.json sw.js \
           generate_icons.py start_dashboard.bat stop_dashboard.bat \
           README.md setup.sh update.sh installer.iss config.ini.example"

info "Updating application files..."
for f in $APP_FILES; do
    [ -f "$f" ] && sudo cp "$f" "$INSTALL_DIR/"
done
sudo chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR"

# ── 4. Refresh Python dependencies (in case new ones were added) ──────────────
info "Updating Python packages..."
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade "flask>=3.1.3" waitress givenergy-modbus Pillow pyopenssl || \
    warn "Package update step had a problem — continuing."

# ── 5. Regenerate icons (in case the generator changed) ───────────────────────
"$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/generate_icons.py" >/dev/null 2>&1 || true

# ── 6. Restart ─────────────────────────────────────────────────────────────────
info "Restarting the dashboard service..."
sudo systemctl start "$SERVICE"
sleep 3

if systemctl is-active --quiet "$SERVICE"; then
    echo ""
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  Update complete!${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""
    echo "  Your settings and history have been preserved."
    echo "  A backup was saved to: $BACKUP"
    echo ""
    echo "  Database schema upgrades (new columns/tables) are applied"
    echo "  automatically on startup — no action needed."
    echo ""
else
    error "Service failed to start. Check: sudo journalctl -u $SERVICE -n 50"
fi
