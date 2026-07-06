#!/usr/bin/env bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "=============================================="
echo "      AGent-Ada Automated Installer           "
echo "=============================================="

# 1. Verify Python version
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 is not installed. Please install Python 3.11+ first." >&2
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
REQUIRED_VERSION="3.11"

if [ "$(printf '%s\n' "$REQUIRED_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED_VERSION" ]; then
    echo "Error: Python version must be 3.11 or higher. Detected: $PYTHON_VERSION" >&2
    exit 1
fi

echo "✔ Verified Python version: $PYTHON_VERSION"

# 2. Virtual Environment Setup
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment (.venv)..."
    python3 -m venv .venv
else
    echo "✔ Virtual environment (.venv) already exists."
fi

# 3. Activate Virtual Environment
echo "Activating virtual environment..."
source .venv/bin/activate

# 4. Upgrade Packaging Tools & Install Dependencies
echo "Upgrading pip, setuptools, and wheel..."
pip install --upgrade pip setuptools wheel

echo "Installing AGent-Ada package and dependencies..."
pip install -e .

# 5. Environment File Setup
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        echo "Creating .env from template (.env.example)..."
        cp .env.example .env
        echo "✔ Created .env configuration file."
    else
        echo "Warning: .env.example template not found. Creating empty .env..."
        touch .env
    fi
else
    echo "✔ Existing .env configuration file found."
fi

echo "=============================================="
echo "✔ Installation completed successfully!"
echo "To get started:"
echo "  1. Run: source .venv/bin/activate"
echo "  2. Configure your keys in .env"
echo "  3. Run: python -m agent.keyless --help"
echo "=============================================="
