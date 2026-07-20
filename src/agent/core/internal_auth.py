"""Shared utilities for internal service-to-service authentication.

Provides HMAC-signed header generation that matches the server's
authenticate() middleware (X-Signature + X-Timestamp), with replay-safe
body binding.  Falls back to DASHBOARD_PASSWORD when INTERNAL_API_SECRET
is unset.
"""

import hashlib
import hmac
import json
import os
import time
from typing import Optional


def get_internal_api_headers(
    method: str,
    path: str,
    query: str = "",
    json_data: Optional[dict] = None,
) -> dict:
    """Build HMAC-signed headers for internal service-to-service API calls.

    Args:
        method: HTTP method (GET, POST, etc.).
        path: URL path (e.g. '/api/subagents/spawn').
        query: Canonical query string (usually empty for internal calls).
        json_data: Optional JSON payload dict — included in body hash.

    Returns:
        A dict of headers ready to pass to aiohttp/httpx requests.
        Returns an empty dict if no credentials are available at all.
    """
    secret_str = os.environ.get("INTERNAL_API_SECRET", "")
    if not secret_str:
        dashboard_password = os.environ.get("DASHBOARD_PASSWORD", "")
        if not dashboard_password:
            return {}  # No credentials available
        secret = hashlib.sha256(dashboard_password.encode()).digest()
    else:
        secret = hashlib.sha256(secret_str.encode()).digest()

    timestamp_str = str(int(time.time()))
    body = b""
    if json_data is not None:
        body = json.dumps(json_data).encode("utf-8")
    body_hash = hashlib.sha256(body).hexdigest()

    message = f"{method.upper()}:{path}:{query}:{timestamp_str}:{body_hash}".encode()
    sig = hmac.new(secret, message, hashlib.sha256).hexdigest()

    headers = {
        "X-Signature": sig,
        "X-Timestamp": timestamp_str,
    }
    if json_data is not None:
        headers["Content-Type"] = "application/json"
    return headers
