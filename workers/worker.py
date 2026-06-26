#!/usr/bin/env python3
"""
Ada Worker — Remote execution agent for the Ada Task Engine.

A lightweight FastAPI server that registers with the Ada hub and executes
tasks dispatched to it. Designed to run on machines with more compute power
(GPU boxes, desktops, datacenter nodes via ZeroTier, etc.).

Configuration via environment variables:
    WORKER_ID           — Unique identifier (default: worker-<hostname>)
    HUB_URL             — Hub's base URL (default: http://localhost:8050)
    WORKER_API_KEY      — Shared secret for auth (required in production)
    WORKER_PORT         — Port to listen on (default: 8051)
    WORKER_CAPABILITIES — Comma-separated capabilities (default: heavy_compute)
    WORKER_MAX_CONCURRENT — Max concurrent tasks (default: 3)
"""

import asyncio
import json
import os
import platform
import shutil
import socket
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WORKER_ID = os.environ.get("WORKER_ID", f"worker-{socket.gethostname()}")
HUB_URL = os.environ.get("HUB_URL", "http://localhost:8050").rstrip("/")
API_KEY = os.environ.get("WORKER_API_KEY", "")
WORKER_PORT = int(os.environ.get("WORKER_PORT", "8051"))
CAPABILITIES = [c.strip() for c in os.environ.get("WORKER_CAPABILITIES", "heavy_compute").split(",") if c.strip()]
MAX_CONCURRENT = int(os.environ.get("WORKER_MAX_CONCURRENT", "3"))

# Track active tasks for concurrency limiting
_active_tasks = 0
_start_time = time.time()
_tasks_completed = 0
_tasks_failed = 0

# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def verify_api_key(request: Request):
    """Validates the shared API key from the X-Worker-Key header."""
    if not API_KEY:
        return  # No key configured = open (LAN-only deployments)
    key = request.headers.get("X-Worker-Key", "")
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

# ---------------------------------------------------------------------------
# Harness path resolution (same logic as keyless.py)
# ---------------------------------------------------------------------------

def _find_binary(name: str) -> Optional[str]:
    """Find agy or grok binary, checking ~/.local/bin as fallback."""
    found = shutil.which(name)
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / name
    if fallback.exists() and os.access(str(fallback), os.X_OK):
        return str(fallback)
    return None

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

async def _register_with_hub():
    """Register this worker with the Ada hub on startup."""
    import httpx
    manifest = {
        "worker_id": WORKER_ID,
        "host": f"{_get_local_ip()}:{WORKER_PORT}",
        "capabilities": CAPABILITIES,
        "platform": platform.system().lower(),
        "python_version": platform.python_version(),
        "max_concurrent": MAX_CONCURRENT,
        "has_agy": _find_binary("agy") is not None,
        "has_grok": _find_binary("grok") is not None,
    }
    headers = {}
    if API_KEY:
        headers["X-Worker-Key"] = API_KEY

    for attempt in range(5):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{HUB_URL}/api/workers/register",
                    json=manifest,
                    headers=headers,
                )
                if resp.status_code == 200:
                    print(f"[WORKER] Registered with hub at {HUB_URL} as '{WORKER_ID}'")
                    return
                else:
                    print(f"[WORKER] Registration failed (HTTP {resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"[WORKER] Registration attempt {attempt + 1}/5 failed: {e}")
        await asyncio.sleep(3 * (attempt + 1))

    print(f"[WORKER] WARNING: Could not register with hub at {HUB_URL}. Running standalone.")

def _get_local_ip() -> str:
    """Best-effort local IP detection for registration."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    print(f"[WORKER] Starting Ada Worker '{WORKER_ID}' on port {WORKER_PORT}")
    print(f"[WORKER] Capabilities: {CAPABILITIES}")
    print(f"[WORKER] Platform: {platform.system()} {platform.machine()}")
    print(f"[WORKER] Hub: {HUB_URL}")
    asyncio.create_task(_register_with_hub())
    yield
    print(f"[WORKER] Shutting down.")

app = FastAPI(title=f"Ada Worker ({WORKER_ID})", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "worker_id": WORKER_ID,
        "platform": platform.system().lower(),
        "arch": platform.machine(),
        "capabilities": CAPABILITIES,
        "active_tasks": _active_tasks,
        "max_concurrent": MAX_CONCURRENT,
        "uptime_seconds": int(time.time() - _start_time),
        "tasks_completed": _tasks_completed,
        "tasks_failed": _tasks_failed,
        "has_agy": _find_binary("agy") is not None,
        "has_grok": _find_binary("grok") is not None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# Execute endpoint — the core work dispatcher
# ---------------------------------------------------------------------------

@app.post("/execute", dependencies=[Depends(verify_api_key)])
async def execute_task(request: Request):
    global _active_tasks, _tasks_completed, _tasks_failed

    if _active_tasks >= MAX_CONCURRENT:
        raise HTTPException(
            status_code=429,
            detail=f"Worker at capacity ({_active_tasks}/{MAX_CONCURRENT} tasks running)"
        )

    body = await request.json()
    prompt = body.get("prompt", "")
    model = body.get("model", "gemini-3.5-flash")
    timeout_val = body.get("timeout", 120.0)
    conversation_id = body.get("conversation_id")
    system_instructions = body.get("system_instructions")

    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    _active_tasks += 1

    async def stream_execution():
        global _active_tasks, _tasks_completed, _tasks_failed
        try:
            # Build the full prompt
            full_prompt = prompt
            if system_instructions:
                full_prompt = f"[System Instructions]\n{system_instructions}\n\n[User Prompt]\n{prompt}"

            # Try agy first, then grok
            harness = _find_binary("agy")
            if harness:
                result = await _run_harness(harness, full_prompt, model, timeout_val, conversation_id)
                if result is not None:
                    yield f"data: {json.dumps({'type': 'chunk', 'content': result, 'worker_id': WORKER_ID})}\n\n"
                    yield "data: [DONE]\n\n"
                    _tasks_completed += 1
                    return

            # Fallback to grok
            grok = _find_binary("grok")
            if grok:
                yield f"data: {json.dumps({'type': 'thought', 'content': f'[Worker {WORKER_ID}] Falling back to grok...'})}\n\n"
                result = await _run_harness(grok, full_prompt, model, timeout_val, conversation_id, is_grok=True)
                if result is not None:
                    yield f"data: {json.dumps({'type': 'chunk', 'content': result, 'worker_id': WORKER_ID})}\n\n"
                    yield "data: [DONE]\n\n"
                    _tasks_completed += 1
                    return

            # No harness available — return error
            yield f"data: {json.dumps({'type': 'error', 'content': f'Worker {WORKER_ID} has no available harness (agy/grok)'})}\n\n"
            yield "data: [DONE]\n\n"
            _tasks_failed += 1

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': f'Worker execution error: {e}'})}\n\n"
            yield "data: [DONE]\n\n"
            _tasks_failed += 1
        finally:
            _active_tasks -= 1

    return StreamingResponse(
        stream_execution(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Worker-Id": WORKER_ID,
        },
    )

async def _run_harness(
    binary: str,
    prompt: str,
    model: str,
    timeout_val: float,
    conversation_id: Optional[str] = None,
    is_grok: bool = False,
) -> Optional[str]:
    """Execute a prompt via agy or grok CLI and return the response text."""
    if is_grok:
        cmd = [binary, "-p", prompt, "--deny", "*", "--no-plan"]
    else:
        cmd = [binary, "-p", prompt, "--dangerously-skip-permissions", "--model", model]
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_val)
        response_text = stdout.decode("utf-8", errors="replace")

        if proc.returncode == 0 and response_text.strip():
            return response_text.strip()
        else:
            err = stderr.decode("utf-8", errors="replace").strip()
            print(f"[WORKER] Harness failed (rc={proc.returncode}): {err or 'empty response'}")
            return None
    except asyncio.TimeoutError:
        print(f"[WORKER] Harness timed out after {timeout_val}s")
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"[WORKER] Harness error: {e}")
        return None

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "worker:app",
        host="0.0.0.0",
        port=WORKER_PORT,
        log_level="info",
    )
