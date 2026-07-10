import os
import logging
from pathlib import Path
from typing import Optional, List
from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# Import the core FastAPI app and authentication dependency
from agent.api.router import app, authenticate, load_plugins, PriorityLock, get_session_lock

# Import routers to register endpoints automatically on startup
import agent.api.chat
from agent.api.chat import get_or_create_agent, check_and_compact_session_history
import agent.api.subagents
import agent.api.skills
import agent.api.plugins
import agent.api.workers

# Ollama-compatible endpoint: disabled by default. Opt-in via ADA_ENABLE_OLLAMA_ENDPOINT=1 in .env.
# When disabled, the /api/ollama/* routes are never registered (404).
# See SECURITY.md for the tradeoff: zero-cost inference vs. OAuth token in sandbox.
if os.environ.get("ADA_ENABLE_OLLAMA_ENDPOINT", "0").strip().lower() in ("1", "true", "yes"):
    import agent.api.ollama_clone
    print("[SECURITY] Ollama-compatible endpoint ENABLED (ADA_ENABLE_OLLAMA_ENDPOINT=1)")
else:
    print("[SECURITY] Ollama-compatible endpoint DISABLED (default). Set ADA_ENABLE_OLLAMA_ENDPOINT=1 in .env to enable.")

# Expose cron/scheduler utilities for backward compatibility
from agent.core.scheduler import get_next_cron_run, ensure_default_scheduled_tasks, fetch_real_quotas_sync, discover_language_server
from agent import tools
from agent.keyless import KeylessAgyAgent

# Mount static files directory
static_dir = Path(__file__).parent.parent / "static"

@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def get_index(username: str = Depends(authenticate)):
    index_path = static_dir / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=False), name="static")

from agent.core.subagent_manager import is_safe_relative_path

def update_session_mapping(session_id: str, new_conv_id: str):
    if not session_id or not session_id.startswith("discord-"):
        return
    try:
        from agent import memory
        mem = memory.load_memory()
        session_mappings = mem.setdefault("key_value", {}).get("session_mappings", {})
        if isinstance(session_mappings, dict) and session_mappings.get(session_id) != new_conv_id:
            session_mappings[session_id] = new_conv_id
            memory.update_key_value("session_mappings", session_mappings)
            print(f"[SESSION MAPPING] Updated mapping for '{session_id}' to '{new_conv_id}'")
    except Exception as e:
        print(f"[SESSION MAPPING] Error updating mapping: {e}")
