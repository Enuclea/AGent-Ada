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

# 6. Check for agy and grok CLI dependencies
echo ""
echo "=============================================="
echo "    Checking CLI Dependencies (agy / grok)    "
echo "=============================================="

HAS_AGY=false
HAS_GROK=false

if command -v agy &>/dev/null; then
    HAS_AGY=true
    echo "✔ Found AntiGravity CLI (agy) at $(which agy)"
else
    echo "❌ AntiGravity CLI (agy) is not installed."
fi

if command -v grok &>/dev/null; then
    HAS_GROK=true
    echo "✔ Found grok CLI at $(which grok)"
else
    echo "❌ grok CLI is not installed."
fi

# Prompt or display instructions for CLI tools if missing
if [ "$HAS_AGY" = false ] || [ "$HAS_GROK" = false ]; then
    echo ""
    echo "These tools are optional but required if you intend to route models keylessly."
    
    # Check if we are running in an interactive terminal
    if [ -t 0 ]; then
        echo "Would you like to install the missing CLI tool(s) now?"
        echo "1) Install AntiGravity CLI (agy)"
        echo "2) Install grok CLI"
        echo "3) Install both"
        echo "4) Skip (I will install them manually later)"
        read -p "Select an option [1-4]: " cli_choice
        
        case "$cli_choice" in
            1)
                echo "Installing AntiGravity CLI (agy)..."
                npm install -g @google/antigravity-cli || echo "⚠️ npm install failed. Please install agy manually."
                ;;
            2)
                echo "Installing grok CLI..."
                npm install -g xai-grok-cli || echo "⚠️ npm install failed. Please install grok manually."
                ;;
            3)
                echo "Installing both CLIs..."
                npm install -g @google/antigravity-cli xai-grok-cli || echo "⚠️ npm install failed. Please install CLI tools manually."
                ;;
            *)
                echo "Skipping CLI installation."
                ;;
        esac
    else
        echo "💡 Install Commands (non-interactive skip):"
        echo "  - To install agy:  npm install -g @google/antigravity-cli"
        echo "  - To install grok: npm install -g xai-grok-cli"
    fi
fi

# Print final authentication guidance
echo ""
echo "=============================================="
echo "✔ Installation completed successfully!"
echo ""
echo "To get started:"
echo "  1. Activate the environment:"
echo "     source .venv/bin/activate"
echo ""
echo "  2. Authenticate CLI routes (if using keyless execution):"
if [ "$HAS_AGY" = false ]; then
    echo "     - AntiGravity (agy): Install and run 'agy auth login'"
else
    echo "     - AntiGravity (agy): Already installed. Run 'agy auth login' to authenticate"
fi
if [ "$HAS_GROK" = false ]; then
    echo "     - Grok:              Install and run 'grok auth login'"
else
    echo "     - Grok:              Already installed. Run 'grok auth login' to authenticate"
fi
echo ""
echo "  3. Configure minimum credentials in .env:"
echo "     - Set GEMINI_API_KEY (if not using keyless agy)"
echo "     - Set DASHBOARD_USERNAME and DASHBOARD_PASSWORD"
echo "     - Set INTERNAL_API_SECRET to secure internal communications"
echo ""
echo "  4. Start the services:"
echo "     ./docker-up.sh"
echo "=============================================="
