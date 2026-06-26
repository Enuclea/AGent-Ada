"""
RemoteWorkerAgent — Dispatches tasks to remote Ada Worker nodes.

Drop-in replacement for KeylessAgyAgent that routes prompts to remote
worker machines via HTTP/SSE. Falls back to local KeylessAgyAgent if
no worker is available or all workers are unreachable.

Usage:
    from agent.remote_worker import RemoteWorkerAgent

    agent = RemoteWorkerAgent(
        required_capabilities=["heavy_compute"],
        model="gemini-3.5-flash",
    )
    response = await agent.chat("Analyze this large dataset...")
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from agent import memory
from agent.keyless import KeylessAgyAgent, KeylessAgyResponse, TaskPriority, _circuit_breaker


# ---------------------------------------------------------------------------
# Worker health and discovery
# ---------------------------------------------------------------------------

def get_healthy_workers(
    required_capabilities: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Returns registered workers that are healthy and have required capabilities."""
    workers = memory.get_registered_workers()
    healthy = []
    for w in workers:
        # Skip offline or unhealthy workers
        if w.get("status") != "online":
            continue
        # Skip workers with open circuits
        if _circuit_breaker.is_open(f"worker:{w['worker_id']}"):
            continue
        # Check capabilities
        if required_capabilities:
            worker_caps = w.get("capabilities", [])
            if isinstance(worker_caps, str):
                worker_caps = [c.strip() for c in worker_caps.split(",")]
            if not all(cap in worker_caps for cap in required_capabilities):
                continue
        # Skip workers at capacity
        active = w.get("active_tasks", 0)
        max_concurrent = w.get("max_concurrent", 3)
        if active >= max_concurrent:
            continue
        healthy.append(w)

    # Sort by load (least loaded first)
    healthy.sort(key=lambda w: w.get("active_tasks", 0))
    return healthy


async def check_worker_health(worker: Dict[str, Any]) -> bool:
    """Pings a worker's /health endpoint. Returns True if reachable."""
    import httpx
    host = worker.get("host", "")
    if not host:
        return False

    # Ensure http:// prefix
    url = host if host.startswith("http") else f"http://{host}"
    api_key = os.environ.get("WORKER_API_KEY", "")
    headers = {}
    if api_key:
        headers["X-Worker-Key"] = api_key

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{url}/health", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                # Update worker stats in DB
                memory.update_worker_health(
                    worker["worker_id"],
                    status="online",
                    active_tasks=data.get("active_tasks", 0),
                )
                return True
    except Exception:
        pass

    memory.update_worker_health(worker["worker_id"], status="offline")
    return False


# ---------------------------------------------------------------------------
# RemoteWorkerAgent
# ---------------------------------------------------------------------------

class RemoteWorkerAgent:
    """Agent that dispatches work to a remote Ada Worker node.

    Has the same async interface as KeylessAgyAgent so callers don't need
    to know whether execution is local or remote.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        system_instructions: Optional[str] = None,
        conversation_id: Optional[str] = None,
        timeout: Optional[float] = None,
        task_priority: TaskPriority = TaskPriority.INTERACTIVE,
        required_capabilities: Optional[List[str]] = None,
        preferred_worker_id: Optional[str] = None,
    ):
        self.model = model or "gemini-3.5-flash"
        self.system_instructions = system_instructions
        self.conversation_id = conversation_id
        self.timeout = timeout or 120.0
        self.task_priority = task_priority
        self.required_capabilities = required_capabilities or []
        self.preferred_worker_id = preferred_worker_id
        self._selected_worker: Optional[Dict[str, Any]] = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def chat(self, prompt: str) -> KeylessAgyResponse:
        """Execute prompt on a remote worker, falling back to local if unavailable."""

        # 1. Find a suitable worker
        worker = await self._select_worker()
        if worker:
            try:
                result = await self._execute_remote(worker, prompt)
                if result is not None:
                    _circuit_breaker.record_success(f"worker:{worker['worker_id']}")
                    return KeylessAgyResponse(result)
            except Exception as e:
                _circuit_breaker.record_failure(f"worker:{worker['worker_id']}")
                print(f"[REMOTE] Worker {worker['worker_id']} failed: {e}. Falling back to local.")

        # 2. Fallback: try other workers
        other_workers = get_healthy_workers(self.required_capabilities)
        for w in other_workers:
            if worker and w["worker_id"] == worker["worker_id"]:
                continue  # Already tried
            try:
                result = await self._execute_remote(w, prompt)
                if result is not None:
                    _circuit_breaker.record_success(f"worker:{w['worker_id']}")
                    return KeylessAgyResponse(result)
            except Exception as e:
                _circuit_breaker.record_failure(f"worker:{w['worker_id']}")
                print(f"[REMOTE] Worker {w['worker_id']} failed: {e}")

        # 3. Final fallback: local KeylessAgyAgent
        print("[REMOTE] No remote workers available. Falling back to local execution.")
        local_agent = KeylessAgyAgent(
            model=self.model,
            system_instructions=self.system_instructions,
            conversation_id=self.conversation_id,
            timeout=self.timeout,
            task_priority=self.task_priority,
        )
        return await local_agent.chat(prompt)

    async def _select_worker(self) -> Optional[Dict[str, Any]]:
        """Select the best worker for this task."""
        # If a specific worker is preferred, try it first
        if self.preferred_worker_id:
            workers = memory.get_registered_workers()
            for w in workers:
                if w["worker_id"] == self.preferred_worker_id:
                    if await check_worker_health(w):
                        self._selected_worker = w
                        return w
                    break

        # Otherwise, find the best available worker with required capabilities
        healthy = get_healthy_workers(self.required_capabilities)
        if healthy:
            # Verify the top candidate is actually reachable
            for w in healthy:
                if await check_worker_health(w):
                    self._selected_worker = w
                    return w
        return None

    async def _execute_remote(
        self, worker: Dict[str, Any], prompt: str
    ) -> Optional[str]:
        """POST a prompt to a remote worker and collect the streamed response."""
        import httpx

        host = worker.get("host", "")
        if not host:
            return None
        url = host if host.startswith("http") else f"http://{host}"

        api_key = os.environ.get("WORKER_API_KEY", "")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-Worker-Key"] = api_key

        payload = {
            "prompt": prompt,
            "model": self.model,
            "timeout": self.timeout,
            "conversation_id": self.conversation_id,
            "system_instructions": self.system_instructions,
        }

        result_chunks = []
        try:
            async with httpx.AsyncClient(timeout=self.timeout + 10) as client:
                async with client.stream(
                    "POST", f"{url}/execute", json=payload, headers=headers
                ) as resp:
                    if resp.status_code != 200:
                        error_body = await resp.aread()
                        raise RuntimeError(
                            f"Worker returned HTTP {resp.status_code}: {error_body.decode()}"
                        )

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            event = json.loads(data_str)
                            if event.get("type") == "chunk":
                                result_chunks.append(event.get("content", ""))
                            elif event.get("type") == "error":
                                raise RuntimeError(
                                    f"Worker error: {event.get('content', 'unknown')}"
                                )
                        except json.JSONDecodeError:
                            continue

        except httpx.TimeoutException:
            raise RuntimeError(f"Worker {worker['worker_id']} timed out")

        if result_chunks:
            return "".join(result_chunks)
        return None


# ---------------------------------------------------------------------------
# Convenience function for the orchestrator
# ---------------------------------------------------------------------------

async def execute_on_worker(
    prompt: str,
    required_capabilities: Optional[List[str]] = None,
    model: str = "gemini-3.5-flash",
    system_instructions: Optional[str] = None,
    timeout: float = 120.0,
    task_priority: TaskPriority = TaskPriority.INTERACTIVE,
) -> Optional[str]:
    """High-level convenience: execute a prompt on a remote worker if available.

    Returns the response text, or None if no worker handled it (caller should
    fall back to local execution).
    """
    agent = RemoteWorkerAgent(
        model=model,
        system_instructions=system_instructions,
        timeout=timeout,
        task_priority=task_priority,
        required_capabilities=required_capabilities,
    )
    try:
        response = await agent.chat(prompt)
        return response.text
    except Exception as e:
        print(f"[REMOTE] execute_on_worker failed: {e}")
        return None
