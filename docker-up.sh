#!/usr/bin/env bash
set -e

# If run with sudo, restore Sudo user's HOME to preserve credentials mapping
if [ -n "$SUDO_USER" ] && [ "$HOME" = "/root" ]; then
    USER_HOME=$(eval echo "~$SUDO_USER")
    export HOME="$USER_HOME"
fi

# Determine the correct docker compose command to use
DOCKER_CMD="sudo -E docker compose"

echo "🚀 Starting AGent Task Engine Docker stack..."
$DOCKER_CMD up -d --build

echo "✔ Docker stack started successfully!"
$DOCKER_CMD ps
