#!/bin/bash
# ============================================================
#  ACBC GivEnergy Dashboard — macOS installer
#  Double-click this file in Finder, or run:  bash setup-mac.command
#  Installs to ~/Library/Application Support, no admin password needed.
# ============================================================
set -euo pipefail

APP_LABEL="com.acbcsoftware.givenergy"
INSTALL_DIR="$HOME/Library/Application Support/ACBCGivEnergyDashboard"
PLIST="$HOME/Library/LaunchAgents/${APP_LABEL}.plist"

# Source dir = wherever this script lives (so it works from the unzipped folder)
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { printf "${GREEN}[INFO]${NC}  %s\n" "$*"; }
warn()  { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
error() { printf "${RED}[ERROR]${NC} %s\n" "$*"; exit 1; }

echo ""
echo "============================================"
echo "  ACBC GivEnergy Dashboard — macOS install"
echo "============================================"
echo ""

# ── 1. Check Python 3 ──────────────────────────────────────────────────────────
info "Checking for Python 3..."
if ! command -v python3 >/dev/null 2>&1; then
    error "Python 3 is not installed.
       Install it from https://www.python.org/downloads/  (recommended),
       or with Homebrew:  brew install python
       Then double-click this installer again."
fi

PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
PY_VER="${PY_MAJOR}.${PY_MINOR}"

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
    error "Python 3.9 or later required (found $PY_VER)."
fi
info "Python $PY_VER — OK"

# ── 2. Install files (preserving config + history on re-install) ───────────────
if [ -f "$INSTALL_DIR/dashboard_server.py" ]; then
    warn "Existing installation found — your config.ini and history.db will be kept."
    launchctl unload "$PLIST" 2>/dev/null || true
fi

info "Installing to:"
info "  $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

# Stash user data, copy new files, restore
_TMP_KEEP="$(mktemp -d)"
[ -f "$INSTALL_DIR/config.ini" ] && cp "$INSTALL_DIR/config.ini" "$_TMP_KEEP/"
[ -f "$INSTALL_DIR/history.db" ] && cp "$INSTALL_DIR/history.db" "$_TMP_KEEP/"

# Copy app files from the source folder (exclude the installer scripts themselves)
for f in dashboard_server.py dashboard.html manifest.json sw.js generate_icons.py config.ini.example; do
    [ -f "$SRC_DIR/$f" ] && cp "$SRC_DIR/$f" "$INSTALL_DIR/"
done

[ -f "$_TMP_KEEP/config.ini" ] && cp "$_TMP_KEEP/config.ini" "$INSTALL_DIR/config.ini"
[ -f "$_TMP_KEEP/history.db" ] && cp "$_TMP_KEEP/history.db" "$INSTALL_DIR/history.db"
rm -rf "$_TMP_KEEP"

# Fresh install: seed config.ini from the example
if [ ! -f "$INSTALL_DIR/config.ini" ]; then
    cp "$INSTALL_DIR/config.ini.example" "$INSTALL_DIR/config.ini"
    warn "Created config.ini from example — set your inverter IP after install."
fi

# ── 3. Python virtual environment ─────────────────────────────────────────────
info "Creating Python virtual environment..."
cd "$INSTALL_DIR"
python3 -m venv venv

info "Installing Python packages (takes a minute)..."
./venv/bin/pip install --quiet --upgrade pip

# Flask version must match the Python version (same rule as Linux installer):
#  • Python 3.14+: givenergy-modbus resolves to async v2 (no click pin); Flask must be >=3.1.3.
#  • Python <3.14: only givenergy-modbus 0.10.x, which pins click==8.0.1; needs older Flask.
PY_TAG=$(( PY_MAJOR * 100 + PY_MINOR ))
if [ "$PY_TAG" -ge 314 ]; then
    FLASK_SPEC="flask>=3.1.3"
else
    FLASK_SPEC="flask>=2.2,<2.3"
fi

if ! ./venv/bin/pip install --quiet "$FLASK_SPEC" waitress givenergy-modbus Pillow pyopenssl; then
    warn "Modbus control library could not be installed on this Python."
    warn "Falling back to monitoring-only (live data works; Gen2 library Control disabled)."
    ./venv/bin/pip install --quiet "$FLASK_SPEC" waitress Pillow pyopenssl
fi

# ── 4. Generate PWA icons ──────────────────────────────────────────────────────
info "Generating app icons..."
./venv/bin/python generate_icons.py

# ── 5. Install launcher .command files into the install dir ────────────────────
info "Installing launcher shortcuts..."
[ -f "$SRC_DIR/start-dashboard.command" ] && cp "$SRC_DIR/start-dashboard.command" "$INSTALL_DIR/"
[ -f "$SRC_DIR/stop-dashboard.command" ]  && cp "$SRC_DIR/stop-dashboard.command"  "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR"/*.command 2>/dev/null || true

# ── 6. launchd service (start on login, restart on crash) ──────────────────────
info "Registering start-on-login service..."
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${APP_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${INSTALL_DIR}/venv/bin/python</string>
        <string>${INSTALL_DIR}/dashboard_server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${INSTALL_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${INSTALL_DIR}/dashboard.log</string>
    <key>StandardErrorPath</key>
    <string>${INSTALL_DIR}/dashboard.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

# ── 7. Done ───────────────────────────────────────────────────────────────────
WEB_PORT=$(grep -E '^\s*web_port' "$INSTALL_DIR/config.ini" 2>/dev/null | head -1 | sed 's/.*=//' | tr -d ' ' || echo 7890)
[ -z "$WEB_PORT" ] && WEB_PORT=7890
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "localhost")

echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Installation complete!${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo "  The dashboard is running and starts automatically at login."
echo ""
echo "  Open in your browser:"
echo "     http://localhost:${WEB_PORT}"
echo "     http://${LAN_IP}:${WEB_PORT}   <-- from your phone on the same network"
echo ""
echo "  Set your inverter IP:  open the ⚡ Settings screen, or edit"
echo "     $INSTALL_DIR/config.ini"
echo ""
echo "  Launchers (in the install folder):"
echo "     start-dashboard.command   stop-dashboard.command"
echo ""
echo -e "${YELLOW}  Default settings password: password  — change it on first use.${NC}"
echo ""

# Offer to open config and the dashboard
open "http://localhost:${WEB_PORT}" 2>/dev/null || true

echo "Press Return to close this window."
read -r _
