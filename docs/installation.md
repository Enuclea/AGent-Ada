# AGent-Ada: Installation Guide

This guide covers the two supported installation methods for the AGent-Ada task engine: the **Local Shell Installer** (for native/developer execution) and the **Docker Container Method** (for containerized deployment).

---

## 📋 Prerequisites

Before starting, ensure your system meets the following requirements:
*   **Operating System**: Linux (recommended) or macOS.
*   **Python**: Version 3.11 or higher.
*   **Git**: Required for cloning repositories and fetching plugins.
*   **Docker & Docker Compose**: Required for containerized execution.
*   **Google AntiGravity CLI (`agy`)**: Configured and authenticated on your `$PATH`.

---

## ⚡ Method 1: Local Shell Installer (Recommended for Developers)

We provide an automated setup script `install.sh` to construct the virtual environment, install local packages, and prepare your `.env` settings.

### 1. Run the Installer
Run the following commands from the root directory:
```bash
# Make the installer executable
chmod +x install.sh

# Run the installer
./install.sh
```

The script will:
*   Verify your Python version is `3.11+`.
*   Create a virtual environment (`.venv`) if it doesn't already exist.
*   Upgrade core packaging utilities (`pip`, `setuptools`, `wheel`).
*   Install all necessary dependencies and register the local package in editable mode (`pip install -e .`).
*   Copy `.env.example` to create your initial `.env` file if not present.

### 2. Activate the Environment
To begin running commands, always activate your virtual environment:
```bash
source .venv/bin/activate
```

### 3. Basic Verification
After editing your keys in `.env`, verify the installation:
```bash
python -m agent.keyless --help
```

---

## 🐳 Method 2: Docker Container Deployment (Recommended for Production)

You can run the web dashboard API and worker daemon inside an isolated Docker container using the provided `Dockerfile` and `docker-compose.yml`.

### 1. Configure the Environment
Ensure your `.env` file is fully configured in the root directory (the compose file automatically loads environment keys from `.env`).

### 2. Launch the Container
Run the following command to build the image and start the container in detached mode:
```bash
docker compose up -d --build
```

### 3. Verify Container Status
Monitor the container logs to ensure the server starts properly:
```bash
docker compose logs -f
```

You can test the server's health status via its API endpoint:
```bash
curl -f http://localhost:8000/api/status
```

---

## ⚙️ Configuration & Secrets Management

Both installation methods rely on the `.env` configuration file in the root workspace directory. Ensure the following environment flags are configured:

```env
# API Keys & Credentials
DISCORD_BOT_TOKEN="your-discord-bot-token"
GEMINI_API_KEY="your-gemini-developer-key"

# Route Customizations
ROUTE_AGY_STATUS="primary"
ROUTE_GROK_STATUS="secondary"
ROUTE_OLLAMA_STATUS="off"

# Loop controls
VERIFICATION_LOOP_MINUTES=10
```
