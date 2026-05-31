#!/usr/bin/env bash
# ============================================================
#  ACBC GivEnergy Dashboard — Linux / Raspberry Pi installer
#  Tested on: Raspberry Pi OS Bookworm (64-bit), Ubuntu 22.04+
#  Run as the user who will own the service, with sudo access:
#       bash setup.sh
# ============================================================
set -euo pipefail

APP_NAME="givenergy-dashboard"
INSTALL_DIR="/opt/givenergy-dashboard"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
RUN_USER="$(whoami)"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── 1. Check Python ────────────────────────────────────────────────────────────
info "Checking Python version..."
if ! command -v python3 &>/dev/null; then
    warn "Python 3 not found — installing..."
    sudo apt-get update -qq
    sudo apt-get install -y python3 python3-pip python3-venv
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
    error "Python 3.9 or later required (found $PY_VER)"
fi
info "Python $PY_VER — OK"

# ── 2. Install files ───────────────────────────────────────────────────────────
# Detect an existing installation — re-running setup is safe (config + history
# are preserved below), but update.sh is the faster path for upgrades.
if [ -f "$INSTALL_DIR/dashboard_server.py" ]; then
    warn "An existing installation was found at $INSTALL_DIR."
    warn "Your config.ini and history.db will be preserved."
    warn "Tip: for a quick upgrade you can use 'bash update.sh' instead."
    sudo systemctl stop "$APP_NAME" 2>/dev/null || true
fi

info "Installing to $INSTALL_DIR ..."
sudo mkdir -p "$INSTALL_DIR"

# Preserve the user's database + settings across a re-install:
# stash them, copy the new files, then restore.
_TMP_KEEP="$(mktemp -d)"
[ -f "$INSTALL_DIR/config.ini" ] && sudo cp "$INSTALL_DIR/config.ini" "$_TMP_KEEP/"
[ -f "$INSTALL_DIR/history.db" ] && sudo cp "$INSTALL_DIR/history.db" "$_TMP_KEEP/"

sudo cp -r ./* "$INSTALL_DIR/"

# Restore preserved files (overwrites anything the copy may have brought in)
[ -f "$_TMP_KEEP/config.ini" ] && sudo cp "$_TMP_KEEP/config.ini" "$INSTALL_DIR/config.ini"
[ -f "$_TMP_KEEP/history.db" ] && sudo cp "$_TMP_KEEP/history.db" "$INSTALL_DIR/history.db"
rm -rf "$_TMP_KEEP"

sudo chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR"

# Copy example config only if config.ini doesn't exist yet (fresh install)
if [ ! -f "$INSTALL_DIR/config.ini" ]; then
    cp "$INSTALL_DIR/config.ini.example" "$INSTALL_DIR/config.ini"
    warn "Created $INSTALL_DIR/config.ini from example."
    warn ">>> Edit it now and set your inverter IP before starting the service."
fi

# ── 3. Python virtual environment ─────────────────────────────────────────────
info "Creating Python virtual environment..."
cd "$INSTALL_DIR"
python3 -m venv venv

info "Installing Python packages (this takes a minute)..."
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet "flask>=3.1.3" waitress givenergy-modbus Pillow pyopenssl

# ── 4. Generate PWA icons ──────────────────────────────────────────────────────
info "Generating PWA icons..."
./venv/bin/python generate_icons.py

# ── 5. Systemd service ────────────────────────────────────────────────────────
info "Installing systemd service..."
sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=ACBC GivEnergy Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python dashboard_server.py
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$APP_NAME"
sudo systemctl start  "$APP_NAME"

# ── 6. Done ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Installation complete!${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo "  Service status:  sudo systemctl status $APP_NAME"
echo "  View logs:       sudo journalctl -u $APP_NAME -f"
echo "  Stop:            sudo systemctl stop $APP_NAME"
echo "  Restart:         sudo systemctl restart $APP_NAME"
echo ""
IFACE_IP=$(hostname -I | awk '{print $1}')
echo "  Dashboard:       http://${IFACE_IP}:$(grep web_port ${INSTALL_DIR}/config.ini | awk -F= '{gsub(/ /,\"\",$2); print $2}' || echo 7890)"
echo ""
echo -e "${YELLOW}  Default settings password: password${NC}"
echo -e "${YELLOW}  Change it via the ⚡ Settings screen on first use.${NC}"
echo ""
