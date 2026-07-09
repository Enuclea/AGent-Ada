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
shared_secret_str = os.environ.get("INTERNAL_API_SECRET", "")
dashboard_password = os.environ.get("DASHBOARD_PASSWORD", "")
is_testing = os.environ.get("TESTING") == "1"

if not is_testing:
    if not dashboard_password or dashboard_password == "admin":
        raise RuntimeError(
            "CRITICAL SECURITY EXCEPTION: Secure execution requires a non-default DASHBOARD_PASSWORD. "
            "Please configure DASHBOARD_PASSWORD in production environment."
        )

if not shared_secret_str:
    if not is_testing:
        raise RuntimeError(
            "CRITICAL SECURITY EXCEPTION: Secure execution requires INTERNAL_API_SECRET to be configured in production. "
            "Please specify a high-entropy secret key."
        )
    shared_secret = hashlib.sha256(b"admin").digest()
else:
    shared_secret = hashlib.sha256(shared_secret_str.encode()).digest()

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

class CacheBodyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        body_parts = []
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.request":
                body_parts.append(message.get("body", b""))
                more_body = message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                break

        raw_body = b"".join(body_parts)
        scope["raw_body"] = raw_body

        sent = False
        async def cached_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {
                    "type": "http.request",
                    "body": raw_body,
                    "more_body": False
                }
            return await receive()

        await self.app(scope, cached_receive, send)

security = HTTPBasic(auto_error=False)

async def authenticate(request: Request, credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    # Bypass health and status checks
    if request.url.path in ("/health", "/health/", "/api/status", "/api/status/"):
        return credentials



    # Unit testing context: allow testclient loopback bypass if explicitly configured under pytest
    if os.environ.get("TESTING") == "1" and os.environ.get("ADA_ALLOW_TEST_BYPASS") == "1" and "pytest" in sys.modules and (not request.client or request.client.host in ("testclient", "127.0.0.1", "localhost", "::1")):
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
                    body = getattr(request, "scope", {}).get("raw_body", b"")
                    if not body:
                        body = await request.body()
                    
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

    # Bearer Token Authentication
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        correct_password = os.environ.get("DASHBOARD_PASSWORD", "admin" if is_testing else "")
        if hmac.compare_digest(token, correct_password):
            return credentials

    # Basic Authentication for standard browser / dashboard clients
    correct_username = os.environ.get("DASHBOARD_USERNAME", "admin")
    correct_password = os.environ.get("DASHBOARD_PASSWORD", "admin" if is_testing else "")
    
    username_ok = hmac.compare_digest(credentials.username.encode(), correct_username.encode()) if credentials else False
    password_ok = hmac.compare_digest(credentials.password.encode(), correct_password.encode()) if credentials else False
    
    if not credentials or not username_ok or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials

app = FastAPI(title="Ada Task Engine Dashboard", lifespan=lifespan, dependencies=[Depends(authenticate)])
app.add_middleware(CacheBodyMiddleware)

@app.get("/health")
async def health_endpoint():
    return {"status": "healthy"}

# CORS disabled for security (all dashboard interactions serve from local origin)

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
