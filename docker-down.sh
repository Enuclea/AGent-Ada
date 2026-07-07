#!/usr/bin/env bash
set -e

DOCKER_CMD="docker compose"

if [ -e "/var/run/docker.sock" ] && [ ! -w "/var/run/docker.sock" ]; then
    DOCKER_CMD="sudo -E docker compose"
fi

echo "🛑 Stopping AGent Task Engine Docker stack..."
$DOCKER_CMD down

echo "✔ Docker stack stopped."
