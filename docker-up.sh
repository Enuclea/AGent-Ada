#!/usr/bin/env bash
set -e

# Determine the correct docker compose command to use
DOCKER_CMD="docker compose"

# Check if we need sudo to access docker socket
if [ -e "/var/run/docker.sock" ] && [ ! -w "/var/run/docker.sock" ]; then
    echo "🔑 Docker socket requires administrative privileges."
    echo "💡 Using 'sudo -E docker compose' to preserve user environment variables (like HOME)."
    DOCKER_CMD="sudo -E docker compose"
fi

echo "🚀 Starting AGent Task Engine Docker stack..."
$DOCKER_CMD up -d --build

echo "✔ Docker stack started successfully!"
$DOCKER_CMD ps
