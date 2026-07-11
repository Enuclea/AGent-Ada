import asyncio
import logging
import json
import time
from abc import ABC, abstractmethod
from typing import Any, Optional, Dict, Tuple, Callable, Awaitable
from agent.storage import db

log = logging.getLogger(__name__)

class BaseCache(ABC):
    @abstractmethod
    async def get(self, key: Any) -> Optional[Any]:
        """Retrieve an item from the cache. Returns None on cache miss or expiration."""
        pass

    @abstractmethod
    async def set(self, key: Any, value: Any, ttl: int) -> None:
        """Store an item in the cache with a specific Time-To-Live (TTL) in seconds."""
        pass

    @abstractmethod
    async def evict(self, key: Any) -> None:
        """Explicitly remove an entry from the cache."""
        pass

    @abstractmethod
    async def clear(self) -> None:
        """Clear all entries from the cache."""
        pass


class FIFOLimitedMemoryCache(BaseCache):
    """Memory-resident dictionary implementing FIFO eviction and TTL checks."""
    def __init__(self, max_size: int = 1000):
        self.cache: Dict[Any, Tuple[float, float, Any]] = {}  # key -> (stored_time, ttl, value)
        self.max_size = max_size
        self._lock = asyncio.Lock()

    async def get(self, key: Any) -> Optional[Any]:
        async with self._lock:
            if key not in self.cache:
                return None
            entry = self.cache[key]
            if len(entry) == 3:
                stored_time, ttl, val = entry
            else:
                stored_time, val = entry
                ttl = 3600.0  # Default fallback TTL

            if asyncio.get_event_loop().time() - stored_time >= ttl:
                self.cache.pop(key, None)
                return None
            return val

    async def set(self, key: Any, value: Any, ttl: int) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            # Cleanup expired items
            expired = []
            for k, entry in self.cache.items():
                if len(entry) == 3:
                    t, limit = entry[0], entry[1]
                else:
                    t, limit = entry[0], 3600.0
                if now - t >= limit:
                    expired.append(k)

            for k in expired:
                self.cache.pop(k, None)

            # Evict oldest if full
            if len(self.cache) >= self.max_size:
                first_key = next(iter(self.cache))
                self.cache.pop(first_key, None)

            self.cache[key] = (now, float(ttl), value)

    async def evict(self, key: Any) -> None:
        async with self._lock:
            self.cache.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self.cache.clear()


class SQLiteCacheBackend(BaseCache):
    """SQLite-backed cache storage to persist across daemon restarts."""
    def __init__(self, db_path: str, table_name: str = "broker_cache"):
        self.db_path = db_path
        self.table_name = table_name
        self._init_db()

    def _init_db(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {self.table_name} (
                        key_str TEXT PRIMARY KEY,
                        value_json TEXT,
                        stored_time REAL,
                        ttl REAL
                    )
                """)
        finally:
            conn.close()

    def _serialize_key(self, key: Any) -> str:
        if isinstance(key, (tuple, list, dict)):
            return json.dumps(key, sort_keys=True)
        return str(key)

    async def get(self, key: Any) -> Optional[Any]:
        import sqlite3
        key_str = self._serialize_key(key)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"SELECT value_json, stored_time, ttl FROM {self.table_name} WHERE key_str = ?",
                (key_str,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            val_json, stored_time, ttl = row
            if time.time() - stored_time >= ttl:
                # Expired
                cursor.execute(f"DELETE FROM {self.table_name} WHERE key_str = ?", (key_str,))
                conn.commit()
                return None
            return json.loads(val_json)
        finally:
            conn.close()

    async def set(self, key: Any, value: Any, ttl: int) -> None:
        import sqlite3
        key_str = self._serialize_key(key)
        val_json = json.dumps(value)
        now = time.time()
        
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                # Clean expired items
                conn.execute(f"DELETE FROM {self.table_name} WHERE (? - stored_time) >= ttl", (now,))
                
                # Insert or replace
                conn.execute(
                    f"INSERT OR REPLACE INTO {self.table_name} (key_str, value_json, stored_time, ttl) VALUES (?, ?, ?, ?)",
                    (key_str, val_json, now, float(ttl))
                )
        finally:
            conn.close()

    async def evict(self, key: Any) -> None:
        import sqlite3
        key_str = self._serialize_key(key)
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute(f"DELETE FROM {self.table_name} WHERE key_str = ?", (key_str,))
        finally:
            conn.close()

    async def clear(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute(f"DELETE FROM {self.table_name}")
        finally:
            conn.close()


class BaseRateLimiter(ABC):
    @abstractmethod
    async def acquire(self, service: str) -> None:
        """Acquire a rate-limit token for target service."""
        pass

    @abstractmethod
    def update_rate(self, service: str, capacity: float, fill_rate: float) -> None:
        """Dynamically configure or adjust limits for a specific service."""
        pass


class TokenBucketRateLimiter(BaseRateLimiter):
    def __init__(self):
        # Token bucket rate limiters: service -> dict
        # capacity: maximum burst size
        # fill_rate: tokens per second
        # Standard defaults for common APIs are populated.
        self.rate_limiters = {
            "atera": {"tokens": 10.0, "capacity": 10.0, "fill_rate": 10.0, "lock": asyncio.Lock()},
            "morgen": {"tokens": 5.0, "capacity": 5.0, "fill_rate": 1.667, "lock": asyncio.Lock()},
            "gmail": {"tokens": 5.0, "capacity": 5.0, "fill_rate": 2.0, "lock": asyncio.Lock()}
        }

    async def acquire(self, service: str) -> None:
        limiter = self.rate_limiters.get(service)
        if not limiter:
            return
            
        while True:
            async with limiter["lock"]:
                now = asyncio.get_event_loop().time()
                if "last_update" in limiter:
                    delta = now - limiter["last_update"]
                    limiter["tokens"] = min(limiter["capacity"], limiter["tokens"] + delta * limiter["fill_rate"])
                limiter["last_update"] = now
                
                if limiter["tokens"] >= 1.0:
                    limiter["tokens"] -= 1.0
                    return
                    
                needed = 1.0 - limiter["tokens"]
                wait_time = needed / limiter["fill_rate"]
                
            await asyncio.sleep(wait_time)

    def update_rate(self, service: str, capacity: float, fill_rate: float) -> None:
        limiter = self.rate_limiters.get(service)
        if limiter:
            limiter["capacity"] = capacity
            limiter["fill_rate"] = fill_rate
            limiter["tokens"] = min(capacity, limiter["tokens"])
        else:
            self.register_service(service, capacity, fill_rate)

    def register_service(self, service: str, capacity: float, fill_rate: float) -> None:
        """Register a new service dynamically with specified capacity and fill-rate limits."""
        if service not in self.rate_limiters:
            self.rate_limiters[service] = {
                "tokens": capacity,
                "capacity": capacity,
                "fill_rate": fill_rate,
                "lock": asyncio.Lock()
            }


class APIBroker:
    """Central broker for managing rate limits, caching, retries, and logging of arbitrary API calls."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        cache: Optional[BaseCache] = None,
        rate_limiter: Optional[BaseRateLimiter] = None
    ):
        self.db_path = db_path or str(db.DB_FILE_PATH)
        self._cache = cache or FIFOLimitedMemoryCache(max_size=1000)
        self._rate_limiter = rate_limiter or TokenBucketRateLimiter()

    @property
    def cache(self):
        """Backwards compatibility property mapping to the internal cache storage."""
        if hasattr(self._cache, "cache"):
            return self._cache.cache
        return {}

    @cache.setter
    def cache(self, val):
        """Backwards compatibility property mapping to the internal cache storage."""
        if hasattr(self._cache, "cache"):
            self._cache.cache = val

    @property
    def rate_limiters(self):
        """Backwards compatibility property mapping to internal rate limiters dict."""
        if hasattr(self._rate_limiter, "rate_limiters"):
            return self._rate_limiter.rate_limiters
        return {}

    def register_service(self, service: str, capacity: float, fill_rate: float) -> None:
        """Helper to dynamically register a rate-limited service on the underlying rate limiter."""
        if hasattr(self._rate_limiter, "register_service"):
            self._rate_limiter.register_service(service, capacity, fill_rate)

    async def call(
        self,
        service: str,
        endpoint: str,
        method: str,
        request_func: Callable[[], Awaitable[Any]],
        params: Optional[dict] = None,
        json_payload: Optional[dict] = None,
        cache_ttl: Optional[int] = None
    ) -> Any:
        """Route call through broker with caching, rate limiting, logging, and retries.
        
        Note: The params and json_payload dictionaries may be modified in-place to adjust 
        page/chunk sizes if transient timeout errors are encountered. Pass a copy 
        (e.g., dict(params)) if you need to protect the original dictionary from mutation.
        """
        method_upper = method.upper()
        cache_key = None
        
        # Check cache if it is a GET request
        ttl = cache_ttl if cache_ttl is not None else 5
        if method_upper == "GET" and ttl > 0:
            params_str = json.dumps(params, sort_keys=True) if params else ""
            payload_str = json.dumps(json_payload, sort_keys=True) if json_payload else ""
            cache_key = (service, endpoint, method_upper, params_str, payload_str)
            
            cached_val = await self._cache.get(cache_key)
            if cached_val is not None:
                db.log_api_call(
                    service=service,
                    endpoint=endpoint,
                    method=method_upper,
                    params_json=params_str or payload_str or None,
                    success=True,
                    duration=0.0,
                    outcome_tags="cache_hit",
                    db_path=self.db_path
                )
                return cached_val

        # Serialize parameters for database logging
        params_str = json.dumps(params, sort_keys=True) if params else ""
        payload_str = json.dumps(json_payload, sort_keys=True) if json_payload else ""
        db_params = params_str or payload_str or None

        # Execute call with retries and rate limiting
        res = await self._execute_with_retry(
            service, endpoint, method_upper, request_func, db_params, params, json_payload
        )

        # Update cache
        if cache_key and ttl > 0:
            await self._cache.set(cache_key, res, ttl)

        return res

    async def _execute_with_retry(
        self,
        service: str,
        endpoint: str,
        method: str,
        request_func: Callable[[], Awaitable[Any]],
        params_json: Optional[str],
        params: Optional[dict] = None,
        json_payload: Optional[dict] = None
    ) -> Any:
        max_retries = 5
        backoff = 1.0
        
        for attempt in range(max_retries):
            await self._rate_limiter.acquire(service)
            start_time = asyncio.get_event_loop().time()
            try:
                res = await request_func()
                duration = asyncio.get_event_loop().time() - start_time
                
                db.log_api_call(
                    service=service,
                    endpoint=endpoint,
                    method=method,
                    params_json=params_json,
                    success=True,
                    duration=duration,
                    db_path=self.db_path
                )
                return res
            except Exception as e:
                duration = asyncio.get_event_loop().time() - start_time
                err_str = str(e)
                
                # Determine if the error is 429 (rate limit) or transient
                is_429 = False
                if "429" in err_str or "rate limit" in err_str.lower() or "too many requests" in err_str.lower():
                    is_429 = True
                
                status_code = getattr(e, "status", None) or getattr(e, "resp_status", None)
                if status_code == 429:
                    is_429 = True
                    
                if hasattr(e, "resp") and getattr(e.resp, "status", None) == 429:
                    is_429 = True
                
                is_transient = is_429
                is_timeout = False
                if not is_transient:
                    # Check for transient network/server issues
                    if (
                        "connection error" in err_str.lower() or 
                        "timeout" in err_str.lower() or 
                        "500" in err_str or 
                        "502" in err_str or 
                        "503" in err_str or 
                        "504" in err_str or
                        isinstance(e, (asyncio.TimeoutError, ConnectionError)) or
                        "clienterror" in e.__class__.__name__.lower()
                    ):
                        is_transient = True
                        if "timeout" in err_str.lower() or isinstance(e, asyncio.TimeoutError):
                            is_timeout = True
                    elif status_code in (500, 502, 503, 504):
                        is_transient = True

                # If it's not transient, or we ran out of retries, log failure and raise
                if not is_transient or attempt == max_retries - 1:
                    db.log_api_call(
                        service=service,
                        endpoint=endpoint,
                        method=method,
                        params_json=params_json,
                        success=False,
                        duration=duration,
                        error=err_str,
                        db_path=self.db_path
                    )
                    raise

                # Auto-reduce chunk/page sizes if timeout occurs
                if is_timeout:
                    reduced = False
                    if params is not None:
                        for key in ["itemsInPage", "maxResults", "max_results", "limit", "pageSize", "page_size"]:
                            if key in params and isinstance(params[key], int):
                                old_val = params[key]
                                new_val = max(10, old_val // 2)
                                if new_val < old_val:
                                    log.warning(
                                        "[APIBroker] Timeout detected for %s %s. Reducing param '%s' from %d to %d.",
                                        service, endpoint, key, old_val, new_val
                                    )
                                    params[key] = new_val
                                    reduced = True
                    
                    if json_payload is not None:
                        for key in ["itemsInPage", "maxResults", "max_results", "limit", "pageSize", "page_size"]:
                            if key in json_payload and isinstance(json_payload[key], int):
                                old_val = json_payload[key]
                                new_val = max(10, old_val // 2)
                                if new_val < old_val:
                                    log.warning(
                                        "[APIBroker] Timeout detected for %s %s. Reducing json_payload key '%s' from %d to %d.",
                                        service, endpoint, key, old_val, new_val
                                    )
                                    json_payload[key] = new_val
                                    reduced = True

                    if reduced:
                        p_str = json.dumps(params, sort_keys=True) if params else ""
                        pl_str = json.dumps(json_payload, sort_keys=True) if json_payload else ""
                        params_json = p_str or pl_str or None

                log.warning("API call to %s %s failed (attempt %d/%d): %s. Retrying in %ss...", service, endpoint, attempt + 1, max_retries, err_str, backoff)
                await asyncio.sleep(backoff)
                backoff *= 2.0


_shared_broker: Optional[APIBroker] = None

def get_shared_broker(db_path: Optional[str] = None) -> APIBroker:
    global _shared_broker
    if _shared_broker is None:
        _shared_broker = APIBroker(db_path=db_path)
    return _shared_broker
