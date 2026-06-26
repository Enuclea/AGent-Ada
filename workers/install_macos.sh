#!/usr/bin/env bash
# --------------------------------------------------------------------------
# Ada Worker — macOS Installer
#
# Sets up the worker as a launchd user agent.
#
# Usage:
#   ./install_macos.sh                                # Interactive
#   HUB_URL=http://10.0.0.5:8050 ./install_macos.sh  # Non-interactive
#
# After install, manage with:
#   launchctl list | grep ada.worker
#   launchctl kickstart -k gui/$(id -u)/com.ada.worker
#   tail -f ~/Library/Logs/ada-worker.log
# --------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.ada-worker}"
WORKER_PORT="${WORKER_PORT:-8051}"
HUB_URL="${HUB_URL:-}"
WORKER_API_KEY="${WORKER_API_KEY:-}"
WORKER_ID="${WORKER_ID:-worker-$(hostname -s)}"
WORKER_CAPABILITIES="${WORKER_CAPABILITIES:-heavy_compute}"

echo "=========================================="
echo " Ada Worker — macOS Installer"
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

# Create log directory
mkdir -p "$HOME/Library/Logs"

# Create launchd plist
echo "[4/4] Installing launchd agent..."
PLIST_PATH="$HOME/Library/LaunchAgents/com.ada.worker.plist"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ada.worker</string>

    <key>ProgramArguments</key>
    <array>
        <string>$INSTALL_DIR/.venv/bin/python</string>
        <string>$INSTALL_DIR/worker.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>WORKER_ID</key>
        <string>$WORKER_ID</string>
        <key>HUB_URL</key>
        <string>$HUB_URL</string>
        <key>WORKER_PORT</key>
        <string>$WORKER_PORT</string>
        <key>WORKER_API_KEY</key>
        <string>$WORKER_API_KEY</string>
        <key>WORKER_CAPABILITIES</key>
        <string>$WORKER_CAPABILITIES</string>
        <key>WORKER_MAX_CONCURRENT</key>
        <string>3</string>
        <key>PATH</key>
        <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/ada-worker.log</string>

    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/ada-worker.err.log</string>

    <key>ThrottleInterval</key>
    <integer>5</integer>
</dict>
</plist>
EOF

# Unload if already running, then load
launchctl bootout "gui/$(id -u)/com.ada.worker" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

echo ""
echo "=========================================="
echo " Installation complete!"
echo "=========================================="
echo ""
echo "  Status:   launchctl list | grep ada.worker"
echo "  Logs:     tail -f ~/Library/Logs/ada-worker.log"
echo "  Restart:  launchctl kickstart -k gui/\$(id -u)/com.ada.worker"
echo "  Stop:     launchctl bootout gui/\$(id -u)/com.ada.worker"
echo "  Config:   $INSTALL_DIR/.env"
echo ""
echo "  Health check: curl http://localhost:$WORKER_PORT/health"
echo ""
