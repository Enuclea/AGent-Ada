# Docker Authentication Guide: Google AntiGravity (`agy`) & xAI (`grok`)

This guide provides templates and instructions on how to securely pass authentication credentials for Google AntiGravity CLI (`agy`) and the xAI CLI (`grok`) to containerized environments (such as Docker and Docker Compose).

---

## 🔑 1. xAI Grok CLI Authentication

In this environment, `grok` is connected via **OAuth** (OIDC flow) rather than an API key. Its credentials (tokens, expiry, refresh tokens) are managed inside `~/.grok/auth.json`. 

Because of this, **you must use the Volume Mount method** to run Grok executions inside Docker. Passing an API key via environment variables is not active in this setup.

### Primary Method: Volume Mount (Required for OAuth Caching)
Mount the host's `.grok` credentials directory into the container. This makes the local OAuth session (`auth.json` and configuration files) accessible to the containerized CLI binary.

*   **Host Credentials Path**: `~/.grok/`
*   **Container Credentials Path**: `/root/.grok/` (or the home directory of the user running inside the container)

**Example docker-compose.yml**:
```yaml
services:
  agent-worker:
    image: agent-ada:latest
    volumes:
      - ${HOME}/.grok:/root/.grok:ro
    environment:
      - ROUTE_GROK_STATUS=secondary
```

*(Note: The volume must be mounted as read-only `:ro` or read-write `:rw` if the container needs to write update/logs back to the host.)*

---

## ⚡ 2. Google AntiGravity CLI (`agy`) Authentication

`agy` relies on Google Sign-In and native system keyrings to manage encrypted session tokens. Running `agy` in a headless Docker environment requires sharing keyrings or configuration directories.

### Option A: Configuration & Session Directory Mount
For local developer setups, you can mount the Antigravity configuration directory to reuse the active CLI session from the host machine.

*   **Host Path**: `~/.gemini/antigravity-cli/`
*   **Container Path**: `/root/.gemini/antigravity-cli/`

**Example docker-compose.yml**:
```yaml
services:
  agent-worker:
    image: agent-ada:latest
    volumes:
      - ${HOME}/.gemini/antigravity-cli:/root/.gemini/antigravity-cli:ro
    environment:
      - ROUTE_AGY_STATUS=primary
```

---

### Option B: Headless System Keyring Sharing
Because `agy` uses the system keyring (`secret-service` or GNOME Keyring) on Linux, containerized instances need access to the D-Bus system bus to resolve keyring credentials.

To share the host keyring with the container, mount the D-Bus socket and set the relevant environment variable.

**Example docker-compose.yml**:
```yaml
services:
  agent-worker:
    image: agent-ada:latest
    volumes:
      - /run/user/1000/bus:/run/user/1000/bus:ro
    environment:
      - DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
      - ROUTE_AGY_STATUS=primary
```
*(Note: Replace `1000` with the UID of the running user on the host system)*

---

## 🐳 3. Unified Docker Compose Template

Below is a complete, production-ready `docker-compose.yml` template that configures credentials for both execution pathways, using volume mounting for Grok's OAuth credentials.

```yaml
version: '3.8'

services:
  agent-ada-worker:
    image: agent-ada:latest
    build:
      context: .
      dockerfile: Dockerfile
    container_name: agent_ada_worker
    restart: unless-stopped
    
    # Environment variables for execution control
    environment:
      - ROUTE_AGY_STATUS=primary
      - ROUTE_GROK_STATUS=secondary
      - GEMINI_API_KEY=${GEMINI_API_KEY}
      - DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}
      - PYTHONUNBUFFERED=1
      
    # Volume mounts to share local credentials and active session states
    volumes:
      # Mount Grok OAuth session and configs (essential in this setup)
      - ${HOME}/.grok:/root/.grok:ro
      
      # Mount AntiGravity CLI local files (except restricted subdirectories)
      - ${HOME}/.gemini/antigravity-cli/scratch:/root/.gemini/antigravity-cli/scratch:rw
      - ${HOME}/.gemini/antigravity-cli/settings.json:/root/.gemini/antigravity-cli/settings.json:ro
      
      # (Optional) Mount D-Bus for native credential manager access
      - /run/user/1000/bus:/run/user/1000/bus:ro
```

---

## 🔍 4. Verification

After launching your container with `docker compose up -d`, you can verify that the credentials are mapped correctly by running test queries through the container's shell:

```bash
# Verify agy route inside the container
docker compose exec agent-ada-worker agy -p "hello" --model gemini

# Verify grok route inside the container (uses the mounted ~/.grok OAuth configuration)
docker compose exec agent-ada-worker grok -p "hello" --model grok-4.3
```

