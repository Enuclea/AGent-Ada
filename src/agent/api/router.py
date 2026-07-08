import asyncio
import os
import sys
import hashlib
import hmac
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Request, Response, HTTPException, Depends, status
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from agent import memory, tools
from agent.core.scheduler import run_scheduler, run_quota_refresh_loop, ensure_default_scheduled_tasks

# Generate or load the derived HMAC shared secret for service-to-service calls
shared_secret = os.environ.get("INTERNAL_API_SECRET", "").encode()
if not shared_secret:
    dashboard_password = os.environ.get("DASHBOARD_PASSWORD", "admin")
    if dashboard_password == "admin":
        print("[WARNING] Security alert: Secure service-to-service communication is using the default password 'admin' as secret. Please configure INTERNAL_API_SECRET or DASHBOARD_PASSWORD.")
    shared_secret = hashlib.sha256(dashboard_password.encode()).digest()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Clear any stale active tasks on startup
    memory.clear_active_tasks()
    
    # Register default background tasks if not already registered
    ensure_default_scheduled_tasks()

    # Startup background loops
    scheduler_task = asyncio.create_task(run_scheduler())
    quota_task = asyncio.create_task(run_quota_refresh_loop())

    # Run any registered startup event handlers from plugins
    for handler in app.router.on_startup:
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler()
            else:
                handler()
        except Exception as e:
            print(f"[LIFESPAN] Error in plugin startup handler: {e}")

    yield

    # Shutdown background loops
    scheduler_task.cancel()
    quota_task.cancel()

security = HTTPBasic(auto_error=False)

async def authenticate(request: Request, credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    # Bypass health and status checks
    if request.url.path in ("/health", "/health/", "/api/status", "/api/status/"):
        return credentials

    # Unit testing context: allow testclient loopback bypass if explicitly configured
    if os.environ.get("TESTING") == "1" and (not request.client or request.client.host in ("testclient", "127.0.0.1", "localhost", "::1")):
        return credentials

    # Secure Service-to-Service: authenticate using X-Signature and X-Timestamp headers
    sig = request.headers.get("X-Signature")
    timestamp_str = request.headers.get("X-Timestamp")
    if sig and timestamp_str:
        try:
            timestamp = int(timestamp_str)
            # 5-minute sliding window check to prevent replay attacks
            if abs(time.time() - timestamp) < 300:
                try:
                    body = await request.body()
                    async def receive():
                        return {"type": "http.request", "body": body, "more_body": False}
                    request._receive = receive
                    
                    body_hash = hashlib.sha256(body).hexdigest()
                    query_str = request.url.query or ""
                    
                    # Try secure signature format (bind method, path, query, timestamp, body_hash)
                    secure_message = f"{request.method}:{request.url.path}:{query_str}:{timestamp_str}:{body_hash}".encode()
                    expected_secure_sig = hmac.new(shared_secret, secure_message, hashlib.sha256).hexdigest()
                    if hmac.compare_digest(sig, expected_secure_sig):
                        return credentials
                except Exception as auth_inner_err:
                    import traceback
                    print(f"[AUTH EXCEPTION] Error during HMAC signature verification:")
                    traceback.print_exc()
        except ValueError:
            pass

    # Basic Authentication for standard browser / dashboard clients
    correct_username = os.environ.get("DASHBOARD_USERNAME", "admin")
    correct_password = os.environ.get("DASHBOARD_PASSWORD", "admin")
    if not credentials or credentials.username != correct_username or credentials.password != correct_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials

app = FastAPI(title="Ada Task Engine Dashboard", lifespan=lifespan, dependencies=[Depends(authenticate)])

@app.get("/health")
async def health_endpoint():
    return {"status": "healthy"}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class PriorityLock:
    """Acquires a lock sequentially based on request priority (lowest integer value = highest priority)."""
    def __init__(self) -> None:
        self._waiters = []  # list of (priority, asyncio.Future)
        self._locked = False
        self._active_fut = None

    async def acquire(self, priority: int) -> None:
        if not self._locked:
            self._locked = True
            return

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._waiters.append((priority, fut))
        self._waiters.sort(key=lambda x: x[0])

        try:
            await fut
        except asyncio.CancelledError:
            self._waiters = [w for w in self._waiters if w[1] != fut]
            if self._active_fut == fut:
                self._active_fut = None
                self._locked = False
                self._release_next()
            raise

    def release(self) -> None:
        self._locked = False
        self._active_fut = None
        self._release_next()

    def _release_next(self) -> None:
        if self._waiters:
            self._locked = True
            priority, fut = self._waiters.pop(0)
            self._active_fut = fut
            if not fut.done():
                fut.set_result(None)

session_locks = {}

def get_session_lock(session_id: str) -> PriorityLock:
    if session_id not in session_locks:
        session_locks[session_id] = PriorityLock()
    return session_locks[session_id]

def load_plugins(app: FastAPI) -> None:
    """Dynamically loads core integrations and web routes from the plugins directories."""
    from agent.core.plugins import plugin_manager
    plugin_manager.load_plugins(app)

load_plugins(app)
