import asyncio
import collections
import os
import posixpath
import sys
import hashlib
import hmac
import time
import threading
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse, urlencode, parse_qsl

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

# In-process sentinel that can only be set by pytest fixtures, never by env vars alone
_ADA_TEST_BYPASS_SENTINEL = object()
_test_bypass_enabled = False

def enable_test_bypass(sentinel):
    """Called by pytest fixtures to enable test auth bypass. Requires the in-process sentinel."""
    global _test_bypass_enabled
    if sentinel is _ADA_TEST_BYPASS_SENTINEL:
        _test_bypass_enabled = True

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

# Replay nonce cache: stores (signature, timestamp) tuples to prevent request replay.
# Uses an OrderedDict as an LRU with a max size and TTL-based eviction.
_REPLAY_CACHE_MAX = 10000
_REPLAY_CACHE_TTL = 310  # slightly longer than the 300s window
_replay_cache = collections.OrderedDict()  # key=(sig,ts) -> insert_time
_replay_lock = threading.Lock()

def _check_and_record_nonce(sig: str, timestamp_str: str) -> bool:
    """Returns True if this (sig, timestamp) is fresh (not replayed). Records it for future checks."""
    key = (sig, timestamp_str)
    now = time.time()
    with _replay_lock:
        # Evict expired entries
        while _replay_cache:
            oldest_key, oldest_time = next(iter(_replay_cache.items()))
            if now - oldest_time > _REPLAY_CACHE_TTL:
                _replay_cache.pop(oldest_key)
            else:
                break
        # Check for replay
        if key in _replay_cache:
            return False  # Replayed request
        # Record and enforce max size
        _replay_cache[key] = now
        if len(_replay_cache) > _REPLAY_CACHE_MAX:
            _replay_cache.popitem(last=False)
    return True

def _normalize_path(raw_path: str) -> str:
    """Normalize URL path to prevent traversal-based HMAC bypass (e.g. /api/../api/chat)."""
    return posixpath.normpath(raw_path) or "/"

def _canonicalize_query(raw_query: str) -> str:
    """Sort query parameters by key for canonical HMAC computation."""
    if not raw_query:
        return ""
    params = parse_qsl(raw_query, keep_blank_values=True)
    params.sort(key=lambda x: x[0])
    return urlencode(params)

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
    normalized_path = _normalize_path(request.url.path)
    if normalized_path in ("/health", "/api/status"):
        return credentials

    # Unit testing context: allow testclient loopback bypass ONLY if the in-process sentinel was activated
    # by a pytest fixture. Environment variables alone are insufficient to enable this bypass.
    if _test_bypass_enabled and is_testing and "pytest" in sys.modules and (
        not request.client or request.client.host in ("testclient", "127.0.0.1", "localhost", "::1")
    ):
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
                    # Normalize path and canonicalize query to prevent traversal/reordering bypass
                    canon_query = _canonicalize_query(request.url.query or "")
                    
                    # Secure signature format: bind method, normalized path, canonical query, timestamp, body_hash
                    secure_message = f"{request.method}:{normalized_path}:{canon_query}:{timestamp_str}:{body_hash}".encode()
                    expected_secure_sig = hmac.new(shared_secret, secure_message, hashlib.sha256).hexdigest()
                    if hmac.compare_digest(sig, expected_secure_sig):
                        # Replay protection: reject if this exact (sig, timestamp) was already used
                        if _check_and_record_nonce(sig, timestamp_str):
                            return credentials
                        # Silent rejection on replay — do not disclose reason
                except Exception:
                    pass  # Silent rejection — do not print tracebacks on auth failures
        except ValueError:
            pass

    # Bearer Token Authentication
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        correct_password = os.environ.get("DASHBOARD_PASSWORD", "")
        if correct_password and hmac.compare_digest(token, correct_password):
            return credentials

    # Basic Authentication for standard browser / dashboard clients
    correct_username = os.environ.get("DASHBOARD_USERNAME", "admin")
    correct_password = os.environ.get("DASHBOARD_PASSWORD", "")
    
    if not correct_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication not configured",
            headers={"WWW-Authenticate": "Basic"},
        )
    
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

@app.api_route("/api/playwright/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD"])
async def lazy_playwright_route_fallback(request: Request, path: str):
    """Dynamic fallback route for Playwright endpoints.
    
    If a request for a Playwright route is received before the plugin is loaded,
    this endpoint loads the Playwright plugin on-demand (which registers its routes)
    and forwards the request to the correct handler.
    """
    from agent.core.plugins import plugin_manager, PluginState
    if "playwright" in plugin_manager.plugins:
        plugin = plugin_manager.plugins["playwright"]
        if plugin.state != PluginState.ACTIVE:
            plugin_manager.load_single_plugin("playwright")
            
    # Now that the plugin has been loaded, locate the registered handler
    for route in app.routes:
        match, child_scope = route.matches(request.scope)
        if match:
            # Skip ourselves to avoid infinite recursion
            if route.endpoint == lazy_playwright_route_fallback:
                continue
            # Execute the matched handler
            response = await route.handle(request.scope, request.receive, request.send)
            return response
            
    raise HTTPException(status_code=404, detail="Playwright route not found.")
