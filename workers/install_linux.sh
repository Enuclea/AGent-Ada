#!/usr/bin/env bash
# --------------------------------------------------------------------------
# Ada Worker — Linux Installer
#
# Sets up the worker as a systemd user service.
#
# Usage:
#   ./install_linux.sh                          # Interactive
#   HUB_URL=http://10.0.0.5:8050 ./install_linux.sh  # Non-interactive
#
# After install, manage with:
#   systemctl --user status ada-worker
#   systemctl --user restart ada-worker
#   journalctl --user -u ada-worker -f
# --------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.ada-worker}"
WORKER_PORT="${WORKER_PORT:-8051}"
HUB_URL="${HUB_URL:-}"
WORKER_API_KEY="${WORKER_API_KEY:-}"
WORKER_ID="${WORKER_ID:-worker-$(hostname)}"
WORKER_CAPABILITIES="${WORKER_CAPABILITIES:-heavy_compute}"

echo "=========================================="
echo " Ada Worker — Linux Installer"
echo "=========================================="
echo ""

# Prompt for hub URL if not set
if [ -z "$HUB_URL" ]; then
    read -rp "Hub URL (e.g., http://192.168.1.10:8050): " HUB_URL
    if [ -z "$HUB_URL" ]; then
        echo "Error: Hub URL is required."
        exit 1
    fi
fi

# Prompt for API key if not set
if [ -z "$WORKER_API_KEY" ]; then
    read -rp "Shared API key (leave blank for LAN-only, no auth): " WORKER_API_KEY
fi

# Prompt for capabilities
read -rp "Capabilities (comma-separated, default: $WORKER_CAPABILITIES): " input_caps
WORKER_CAPABILITIES="${input_caps:-$WORKER_CAPABILITIES}"

echo ""
echo "Configuration:"
echo "  Install dir:   $INSTALL_DIR"
echo "  Worker ID:     $WORKER_ID"
echo "  Hub URL:       $HUB_URL"
echo "  Port:          $WORKER_PORT"
echo "  Capabilities:  $WORKER_CAPABILITIES"
echo "  API Key:       ${WORKER_API_KEY:+(set)}"
echo ""

# Create install directory
mkdir -p "$INSTALL_DIR"

# Copy worker files
cp "$SCRIPT_DIR/worker.py" "$INSTALL_DIR/worker.py"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"

# Create virtual environment
echo "[1/4] Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/.venv"
source "$INSTALL_DIR/.venv/bin/activate"

echo "[2/4] Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r "$INSTALL_DIR/requirements.txt"

# Create .env file
echo "[3/4] Writing configuration..."
cat > "$INSTALL_DIR/.env" <<EOF
WORKER_ID=$WORKER_ID
HUB_URL=$HUB_URL
WORKER_PORT=$WORKER_PORT
WORKER_API_KEY=$WORKER_API_KEY
WORKER_CAPABILITIES=$WORKER_CAPABILITIES
WORKER_MAX_CONCURRENT=3
EOF

# Create systemd user service
echo "[4/4] Installing systemd user service..."
mkdir -p "$HOME/.config/systemd/user"

cat > "$HOME/.config/systemd/user/ada-worker.service" <<EOF
[Unit]
Description=Ada Worker (Remote Execution Agent)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/worker.py
Restart=always
RestartSec=5

# Ensure PATH includes common binary locations
Environment="PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

[Install]
WantedBy=default.target
EOF

# Enable lingering so user services run without login
loginctl enable-linger "$(whoami)" 2>/dev/null || true

# Reload and enable
systemctl --user daemon-reload
systemctl --user enable ada-worker.service
systemctl --user start ada-worker.service

echo ""
echo "=========================================="
echo " Installation complete!"
echo "=========================================="
echo ""
echo "  Status:   systemctl --user status ada-worker"
echo "  Logs:     journalctl --user -u ada-worker -f"
echo "  Restart:  systemctl --user restart ada-worker"
echo "  Stop:     systemctl --user stop ada-worker"
echo "  Config:   $INSTALL_DIR/.env"
echo ""
echo "  Health check: curl http://localhost:$WORKER_PORT/health"
echo ""
