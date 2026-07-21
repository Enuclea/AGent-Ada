"""Keyless agent routing and execution module.

This module handles routing prompts to different model backends (Gemini,
Anthropic, OpenAI, local Ollama, or fallback Grok) using a failover chain
determined by task priority, model availability, and remaining quotas.
It also manages circuit breakers to bypass temporarily failing models.
"""

import os
import shutil
import asyncio
import uuid
import glob
import json
import re
import time
from pathlib import Path
from typing import Optional, List, Dict, Any, Union, Tuple, AsyncIterator
from google.antigravity.models import GeminiAPIEndpoint

from agent.routes.base import TaskPriority, get_harness_path
from agent.core.routing import routing_engine


class CircuitBreaker:
    """Tracks consecutive failures per model and skips models in 'open' state.
    
    After `failure_threshold` consecutive failures, the circuit opens for
    `reset_seconds` seconds, during which the model is skipped.
    """

    def __init__(self, failure_threshold: int = 3, reset_seconds: float = 300.0) -> None:
        """Initialize circuit breaker with threshold and reset interval.

        Args:
            failure_threshold: Consecutive failures required to open circuit.
            reset_seconds: Time to keep circuit open in seconds.
        """
        self._failures: Dict[str, int] = {}
        self._open_until: Dict[str, float] = {}
        self._failure_threshold: int = failure_threshold
        self._reset_seconds: float = reset_seconds

    def record_failure(self, model: str) -> None:
        """Record a failure for the specified model.

        If failure threshold is reached, open the circuit.

        Args:
            model: The identifier of the model.
        """
        self._failures[model] = self._failures.get(model, 0) + 1
        if self._failures[model] >= self._failure_threshold:
            self._open_until[model] = time.monotonic() + self._reset_seconds

    def record_success(self, model: str) -> None:
        """Record a successful execution for the model, resetting its breaker.

        Args:
            model: The identifier of the model.
        """
        self._failures.pop(model, None)
        self._open_until.pop(model, None)

    def is_open(self, model: str) -> bool:
        """Return True if the model circuit is open (should be skipped).

        Args:
            model: The identifier of the model.
        """
        deadline: Optional[float] = self._open_until.get(model)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            # Circuit has reset — allow retry
            self._open_until.pop(model, None)
            self._failures.pop(model, None)
            return False
        return True


# Module-level circuit breaker instance shared across all agents
_circuit_breaker: CircuitBreaker = CircuitBreaker(failure_threshold=3, reset_seconds=300.0)

# Failover pools — Gemini models share one quota bucket, 3P models share another
GEMINI_POOL: List[str] = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-pro"]
THREE_P_POOL: List[str] = ["Claude Sonnet 4.6 (Thinking)", "gpt-4o"]


class KeylessGeminiAPIEndpoint(GeminiAPIEndpoint):
    """Bypasses client-side API key validation for Gemini Developer API.

    This allows routing to keyless / Ultra plan gateways via Go localharness/agy.
    """

    def validate_endpoint(self) -> None:
        """Validate the endpoint configuration. Overridden to bypass key validation."""
        pass


def get_process_activity_metrics(pid: int) -> Tuple[int, List[int]]:
    """Retrieves total user+sys CPU ticks and child process PIDs on Linux.

    Returns:
        (total_cpu_ticks, child_pids)
    """
    total_ticks = 0
    child_pids = []
    
    # 1. Get CPU ticks for the parent PID
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as f:
            stat_parts = f.read().split()
            # utime=14, stime=15, cutime=16, cstime=17 (indices 13 to 16 inclusive)
            if len(stat_parts) >= 17:
                total_ticks = sum(int(x) for x in stat_parts[13:17])
    except Exception:
        pass
        
    # 2. Get child PIDs
    try:
        for pid_str in os.listdir("/proc"):
            if not pid_str.isdigit():
                continue
            try:
                with open(f"/proc/{pid_str}/stat", "r", encoding="utf-8", errors="replace") as f:
                    stat_content = f.read()
                r_paren = stat_content.rfind(")")
                if r_paren != -1:
                    after_comm = stat_content[r_paren + 2:].split()
                    if len(after_comm) >= 2:
                        parent_pid = int(after_comm[1])
                        if parent_pid == pid:
                            child_pids.append(int(pid_str))
            except Exception:
                continue
    except Exception:
        pass
        
    return total_ticks, child_pids


def has_pending_approvals() -> bool:
    """Checks if there are any active tasks waiting for user approval in the database."""
    try:
        from agent.storage.db import get_connection, DB_FILE_PATH
        conn = get_connection(DB_FILE_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM active_tasks WHERE status = 'pending_approval'")
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        return False


class KeylessAgyResponse:
    """Wrapper class representing the response from keyless agent execution.

    Handles both string responses and streaming subprocess responses,
    consuming stdout/stderr asynchronously.
    """

    def __init__(
        self,
        text_or_proc: Union[str, Any],
        timeout_val: Optional[float] = None,
        agent: Optional[Any] = None,
        prev_newest: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> None:
        """Initialize response wrapper.

        Args:
            text_or_proc: The plain text response or the streaming process.
            timeout_val: The timeout duration in seconds for the process.
            agent: The keyless agent instance triggering the request.
            prev_newest: The previously newest conversation ID.
        """
        import asyncio
        self.task_id = task_id
        if isinstance(text_or_proc, str):
            from agent.security.pipeline import sanitize_output
            sanitized_text = sanitize_output(text_or_proc)
            self.text: str = sanitized_text
            self._chunks: List[str] = [sanitized_text]
            self.proc: Optional[Any] = None
            self.timeout_val: Optional[float] = None
            self.agent: Optional[Any] = None
            self.prev_newest: Optional[str] = None
            self.stdout_lines: List[str] = [sanitized_text]
            self.stderr_lines: List[str] = []
            self._completed_event: asyncio.Event = asyncio.Event()
            self._completed_event.set()
            if task_id:
                try:
                    from agent.core import task_manager
                    task_manager.update_active_task_status(task_id, "completed")
                except Exception:
                    pass
        else:
            self.proc = text_or_proc
            self.timeout_val = timeout_val
            self.agent = agent
            self.prev_newest = prev_newest
            self.text = ""
            self.stdout_lines = []
            self.stderr_lines = []
            self._chunks = []
            self._completed_event = asyncio.Event()
        self._index: int = 0

    async def _consume_stream(self) -> None:
        """Consume stdout and stderr of the process asynchronously and update state."""
        async for _ in self.thoughts:
            pass

    @property
    def thoughts(self) -> AsyncIterator[str]:
        """Stream process thoughts/output lines asynchronously.

        Yields:
            Chunks of response output lines or timeout/error notices.
        """
        async def _stream_thoughts() -> AsyncIterator[str]:
            from agent.security.pipeline import sanitize_output
            if self._completed_event.is_set():
                for line in self.stdout_lines:
                    yield sanitize_output(line)
                return
                
            start_time: float = asyncio.get_event_loop().time()
            last_activity_time: float = start_time
            max_duration: float = self.timeout_val if self.timeout_val is not None else 120.0
            prev_ticks, _ = get_process_activity_metrics(self.proc.pid) if self.proc else (0, [])
            idle_timeout: float = 120.0
            
            # Keep a buffer of the accumulated stdout text to run sanitization across chunk boundaries
            accumulated = ""
            yielded_len = 0

            def get_sanitized_chunk(text: str) -> str:
                nonlocal accumulated, yielded_len
                accumulated += text
                sanitized = sanitize_output(accumulated)
                chunk = sanitized[yielded_len:]
                yielded_len = len(sanitized)
                return chunk

            try:
                while True:
                    current_time = asyncio.get_event_loop().time()
                    elapsed: float = current_time - start_time
                    remaining: float = max_duration - elapsed
                    if remaining <= 0:
                        raise asyncio.TimeoutError(f"Overall session timeout of {max_duration} seconds exceeded")
                    
                    # Check if yield has been requested (e.g. subagent was spawned)
                    # Give a short grace period for the subprocess to finish its current output
                    try:
                        from agent.execution.tools.constants import yield_requested
                        if yield_requested.get():
                            print(f"[YIELD] yield_requested detected in _stream_thoughts. Draining with 5s grace...")
                            grace_end = asyncio.get_event_loop().time() + 5.0
                            while asyncio.get_event_loop().time() < grace_end:
                                try:
                                    chunk_bytes = await asyncio.wait_for(self.proc.stdout.read(4096), timeout=1.0)
                                    if not chunk_bytes:
                                        break
                                    decoded = chunk_bytes.decode("utf-8", errors="replace")
                                    self.stdout_lines.append(decoded)
                                    yield get_sanitized_chunk(decoded)
                                except asyncio.TimeoutError:
                                    if self.proc.returncode is not None:
                                        break
                                    continue
                            print(f"[YIELD] Grace period complete. Breaking stream.")
                            break
                    except (LookupError, ImportError):
                        pass

                    # Use a short timeout of 2.0 seconds to keep polling and check activity metrics
                    chunk_timeout: float = min(2.0, remaining)
                    try:
                        chunk_bytes: bytes = await asyncio.wait_for(self.proc.stdout.read(4096), timeout=chunk_timeout)
                        if not chunk_bytes:
                            break
                        decoded: str = chunk_bytes.decode("utf-8", errors="replace")
                        self.stdout_lines.append(decoded)
                        yield get_sanitized_chunk(decoded)
                        last_activity_time = asyncio.get_event_loop().time()
                    except asyncio.TimeoutError:
                        if self.proc.returncode is not None:
                            break
                        # Yield a blank keep-alive chunk to reset connection inactivity timers
                        yield ""
                        
                        curr_ticks, children = get_process_activity_metrics(self.proc.pid) if self.proc else (0, [])
                        ticks_increased = curr_ticks > prev_ticks
                        prev_ticks = curr_ticks
                        
                        is_active = (
                            ticks_increased or 
                            len(children) > 0 or 
                            has_pending_approvals()
                        )
                        
                        if is_active:
                            last_activity_time = asyncio.get_event_loop().time()
                        else:
                            silence_duration = asyncio.get_event_loop().time() - last_activity_time
                            if silence_duration > idle_timeout:
                                raise asyncio.TimeoutError(f"Inactivity timeout exceeded: {silence_duration:.1f}s of silence")
            except asyncio.TimeoutError as e:
                try:
                    self.proc.kill()
                    await self.proc.wait()
                except Exception:
                    pass
                err_msg: str = f"\n[{e}]\n"
                self.stdout_lines.append(err_msg)
                if self.task_id:
                    try:
                        from agent.core import task_manager
                        task_manager.update_active_task_status(self.task_id, "failed")
                    except Exception:
                        pass
                yield get_sanitized_chunk(err_msg)
            except asyncio.CancelledError:
                if self.task_id:
                    try:
                        from agent.core import task_manager
                        task_manager.update_active_task_status(self.task_id, "failed")
                    except Exception:
                        pass
                raise
            except Exception as e:
                if self.task_id:
                    try:
                        from agent.core import task_manager
                        task_manager.update_active_task_status(self.task_id, "failed")
                    except Exception:
                        pass
                err_msg = f"\n[Error: {e}]\n"
                self.stdout_lines.append(err_msg)
                yield get_sanitized_chunk(err_msg)

            try:
                stderr_data: bytes = await self.proc.stderr.read()
                if stderr_data:
                    self.stderr_lines.append(stderr_data.decode("utf-8", errors="replace"))
            except Exception:
                pass

            try:
                await self.proc.wait()
            except Exception:
                pass

            if self.agent:
                curr_newest: Optional[str] = self.agent._get_newest_conversation_id()
                if curr_newest and curr_newest != self.prev_newest:
                    self.agent.conversation_id = curr_newest
                elif not self.agent.conversation_id and curr_newest:
                    self.agent.conversation_id = curr_newest
                    
            self.text = sanitize_output("".join(self.stdout_lines))
            self._completed_event.set()
            if self.task_id:
                try:
                    from agent.core import task_manager
                    if task_manager.get_active_task_status(self.task_id) == "running":
                        task_manager.update_active_task_status(self.task_id, "completed")
                except Exception:
                    pass

        return _stream_thoughts()

    def __aiter__(self) -> "KeylessAgyResponse":
        """Return self to satisfy the async iterator protocol."""
        return self

    async def __anext__(self) -> str:
        """Fetch the next response chunk asynchronously.

        Returns:
            The complete response text upon process completion.

        Raises:
            StopAsyncIteration: When iteration is finished.
        """
        if self._index >= 1:
            raise StopAsyncIteration
        self._index += 1
        
        await self._consume_stream()
        
        stderr_text: str = "".join(self.stderr_lines).strip()
        if self.proc and self.proc.returncode != 0 and stderr_text:
            return self.text + f"\n\n[Process exited with code {self.proc.returncode}]\nError output:\n{stderr_text}"
            
        return self.text or "Execution completed."

    async def structured_output(self) -> Dict[str, Any]:
        """Parse structured JSON from the response text.

        Extracts JSON block if formatted inside markdown code blocks,
        falling back to finding curly braces, or returning error reason.

        Returns:
            Decoded dictionary representing the structured output.
        """
        await self._consume_stream()
        text: str = self.text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        
        pattern: str = r"```(?:json)?\s*(\{.*?\})\s*```"
        match: Optional[re.Match] = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
                
        match = re.search(r"(\{.*\})", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
                
        return {
            "action_required": False,
            "importance_reason": f"Failed to parse model JSON: {text}",
            "task_title": "",
            "task_description": ""
        }


def get_grok_path() -> Optional[str]:
    """Resolve the path to the system-wide grok binary.

    Checks system PATH first, then falls back to user local bin.

    Returns:
        The resolved binary path, or None if not found.
    """
    grok_path: Optional[str] = shutil.which("grok")
    if grok_path:
        return grok_path
        
    user_home: Path = Path.home()
    fallback_path: Path = user_home / ".local" / "bin" / "grok"
    if fallback_path.exists() and fallback_path.is_file():
        return str(fallback_path)
            
    return None


def check_and_record_grok_usage(db_path: Optional[str] = None) -> bool:
    """Check if grok fallback usage is within rate limits.

    Limits are capped at max 5 calls per hour, and max 20 calls per day.
    Logs current usage timestamps to a JSON config file.

    Args:
        db_path: Unused parameter kept for API compatibility.

    Returns:
        True if the call is within limits, False otherwise.
    """
    import json
    import time
    from pathlib import Path
    
    path: Path = Path.home() / ".agent" / "grok_fallback_calls.json"
    try:
        if path.exists():
            with open(path, "r") as f:
                calls: List[float] = json.load(f)
        else:
            calls = []
    except Exception:
        calls = []
        
    now: float = time.time()
    one_day_ago: float = now - 86400
    one_hour_ago: float = now - 3600
    
    # Clean up older timestamps
    calls = [t for t in calls if t > one_day_ago]
    
    hour_calls: List[float] = [t for t in calls if t > one_hour_ago]
    
    if len(hour_calls) >= 5:
        return False
    if len(calls) >= 20:
        return False
        
    calls.append(now)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(calls, f)
    except Exception:
        pass
    return True


class KeylessAgyAgent:
    """Keyless execution agent wrapping the Google AntiGravity SDK router.

    Automates model fallback, direct API routing, quota check,
    and grok binary invocation.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        system_instructions: Optional[str] = None,
        conversation_id: Optional[str] = None,
        response_schema: Optional[Any] = None,
        db_path: Optional[str] = None,
        timeout: Optional[float] = None,
        task_priority: TaskPriority = TaskPriority.INTERACTIVE,
        cwd: Optional[str] = None,
        roleplay: bool = False,
        general_chat: bool = False,
        disable_tools: bool = False,
    ) -> None:
        """Initialize KeylessAgyAgent.

        Args:
            model: The target model name.
            system_instructions: Instructions for the agent behavior.
            conversation_id: Resume conversation with this session ID.
            response_schema: Optional Pydantic schema for structured output.
            db_path: Optional path to the session database.
            timeout: Process timeout in seconds.
            task_priority: Execution priority of the task.
            cwd: Working directory context for the execution process.
            roleplay: Whether to activate local Ollama fallback for roleplay.
            general_chat: Whether to use casual conversational mode (no protocol).
            disable_tools: Toggle to disable tool use capabilities.
        """
        self.model: Optional[str] = model
        from agent.core.constants import COMMON_PROTOCOL
        common_protocol = COMMON_PROTOCOL
        if roleplay or general_chat or disable_tools:
            self.system_instructions = system_instructions or ""
        elif system_instructions:
            if "[SYSTEM PROTOCOL - TIMEOUT PREVENTION & YIELDING]" not in system_instructions:
                self.system_instructions = common_protocol + system_instructions
            else:
                self.system_instructions = system_instructions
        else:
            self.system_instructions = common_protocol
        self.conversation_id: Optional[str] = conversation_id
        self.response_schema: Optional[Any] = response_schema
        self.db_path: Optional[str] = db_path
        self.timeout: Optional[float] = timeout
        self.task_priority: TaskPriority = task_priority
        self.cwd: Optional[str] = cwd
        self.roleplay: bool = roleplay
        self._conversations_dir: str = str(Path.home() / ".gemini" / "antigravity-cli" / "conversations")

    async def __aenter__(self) -> "KeylessAgyAgent":
        """Enter async context manager block."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit async context manager block."""
        pass

    def _get_newest_conversation_id(self) -> Optional[str]:
        """Scan the conversations directory and return the newest session database ID."""
        if not os.path.exists(self._conversations_dir):
            return None
        db_files: List[str] = glob.glob(os.path.join(self._conversations_dir, "*.db"))
        if not db_files:
            return None
        db_files.sort(key=os.path.getmtime, reverse=True)
        return os.path.basename(db_files[0])[:-3]

    async def _call_direct_api(self, model_name: str, full_prompt: str, bypass_sanitization: bool = False) -> Optional[str]:
        """Attempt to call direct provider APIs if API keys are found in environment.

        Args:
            model_name: Name of the model to use.
            full_prompt: Fully formatted prompt text.
            bypass_sanitization: Whether to skip input security validation (e.g. for safe reviews).

        Returns:
            The API string response if successful, or None on failure.
        """
        import aiohttp
        from agent.security.pipeline import SecurityPipeline
        
        if not bypass_sanitization:
            pipeline = SecurityPipeline()
            sanitized_prompt = pipeline.sanitize_input(full_prompt)
        else:
            sanitized_prompt = full_prompt
        
        # 1. Gemini direct API
        gemini_key: Optional[str] = os.environ.get("GEMINI_API_KEY")
        if gemini_key and ("gemini" in model_name.lower() or model_name == "default"):
            actual_model: str = model_name if "gemini" in model_name.lower() else "gemini-1.5-flash"
            url: str = f"https://generativelanguage.googleapis.com/v1beta/models/{actual_model}:generateContent?key={gemini_key}"
            headers: Dict[str, str] = {"Content-Type": "application/json"}
            payload: Dict[str, Any] = {"contents": [{"parts": [{"text": sanitized_prompt}]}]}
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=headers, json=payload, timeout=self.timeout or 30.0) as resp:
                        if resp.status == 200:
                            data: Dict[str, Any] = await resp.json()
                            res = data["candidates"][0]["content"]["parts"][0]["text"]
                            return pipeline.sanitize_output(res)
            except Exception as e:
                print(f"[DIRECT-API] Gemini API call failed: {e}")

        # 2. Anthropic direct API
        anthropic_key: Optional[str] = os.environ.get("ANTHROPIC_API_KEY")
        if anthropic_key and ("claude" in model_name.lower() or "sonnet" in model_name.lower()):
            actual_model = "claude-3-5-sonnet-20241022" if "sonnet" in model_name.lower() else model_name
            url = "https://api.anthropic.com/v1/messages"
            headers = {
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            }
            payload = {
                "model": actual_model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": sanitized_prompt}]
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=headers, json=payload, timeout=self.timeout or 30.0) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            res = data["content"][0]["text"]
                            return pipeline.sanitize_output(res)
            except Exception as e:
                print(f"[DIRECT-API] Anthropic API call failed: {e}")

        # 3. OpenAI direct API
        openai_key: Optional[str] = os.environ.get("OPENAI_API_KEY")
        if openai_key and ("gpt" in model_name.lower() or "openai" in model_name.lower()):
            actual_model = "gpt-4o" if "gpt" in model_name.lower() else model_name
            url = "https://api.openai.com/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": actual_model,
                "messages": [{"role": "user", "content": sanitized_prompt}]
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=headers, json=payload, timeout=self.timeout or 30.0) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            res = data["choices"][0]["message"]["content"]
                            return pipeline.sanitize_output(res)
            except Exception as e:
                print(f"[DIRECT-API] OpenAI API call failed: {e}")

        # 4. Ollama local API
        ollama_host: str = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        if "ollama" in model_name.lower() or os.environ.get("USE_OLLAMA") == "true":
            actual_model = model_name.replace("ollama/", "") if "/" in model_name else "llama3"
            url = f"{ollama_host}/api/generate"
            payload = {
                "model": actual_model,
                "prompt": sanitized_prompt,
                "stream": False
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=self.timeout or 60.0) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            res = data["response"]
                            return pipeline.sanitize_output(res)
            except Exception as e:
                print(f"[LOCAL-OLLAMA] Ollama call failed: {e}")
                
        return None

    async def chat(self, prompt: str) -> KeylessAgyResponse:
        """Route the user prompt to one of the configured backend model APIs.

        Ensures pre-flight checks, handles failover chain loops, checks low-quota,
        falls back to Grok or local APIs.

        Args:
            prompt: The text prompt to query.

        Returns:
            A KeylessAgyResponse wrapper around the response content.
        """
        import uuid
        from agent.core import task_manager
        from agent.security.pipeline import sanitize_input
        
        # Sanitize incoming user prompt
        prompt = sanitize_input(prompt)
        
        agent_name = "Ada"
        if self.conversation_id:
            parts = self.conversation_id.split("-")
            if parts:
                prefix = parts[0]
                import re
                if prefix.lower() in ("boardroom", "sched", "task", "subagent") and len(parts) > 1:
                    prefix = parts[1]
                if not re.match(r"^[0-9a-fA-F]{8}$", prefix) and not re.match(r"^[0-9a-fA-F]{12}$", prefix):
                    agent_name = prefix.replace("_", " ").title()
                    
        # Check system instructions override
        sys_inst = self.system_instructions or ""
        if "Lacie" in sys_inst:
            agent_name = "Lacie"
        elif "Kira" in sys_inst:
            agent_name = "Kira"
        elif "Val" in sys_inst:
            agent_name = "Val"

        task_id = f"task-agent-{uuid.uuid4()}"
        # Extract a meaningful one-liner for the activity feed (skip system driver metadata)
        snippet_lines = prompt.strip().split("\n")
        snippet = ""
        skip_prefixes = ("[SYSTEM DRIVER", "[SYSTEM RESUME", "CRITICAL INSTRUCTIONS", "IMPORTANT:", "The recommended tool", "Note:", "Please execute this step")
        for line in snippet_lines:
            line = line.strip()
            if not line or line.startswith(skip_prefixes) or line.startswith("You are executing") or line.startswith("You have ") or line.startswith("Tasks:"):
                continue
            if line.startswith("Original user request:"):
                snippet = line.replace("Original user request:", "").strip()
                break
            if line.startswith("- Task "):
                snippet = line.lstrip("- Task 0123456789: ").strip()
                break
            snippet = line
            break
        if not snippet:
            snippet = snippet_lines[0].strip() if snippet_lines else "Processing..."
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
            
        task_manager.add_active_task(task_id, agent_name, f"Executing: {snippet}")

        try:
            full_prompt: str = prompt
            if self.system_instructions:
                full_prompt = f"[System Instructions]\n{self.system_instructions}\n\n[User Prompt]\n{prompt}"

            # If response_schema is specified, request JSON format
            if self.response_schema:
                schema_instructions: str = (
                    "\n\nYou MUST return the response ONLY as a raw JSON object. Do not include markdown code block formatting (such as ```json). "
                    "Do not include any explanation or extra text. The JSON object structure MUST match:\n"
                )
                if hasattr(self.response_schema, "model_fields"):
                    schema_fields: Dict[str, Any] = self.response_schema.model_fields
                elif hasattr(self.response_schema, "__fields__"):
                    schema_fields = self.response_schema.__fields__
                else:
                    schema_fields = {}
                
                sample_dict: Dict[str, Any] = {}
                for name, field in schema_fields.items():
                    annotation: Any = getattr(field, "annotation", str)
                    if annotation is bool:
                        sample_dict[name] = True
                    elif annotation is int:
                        sample_dict[name] = 0
                    else:
                        sample_dict[name] = "string"
                schema_instructions += json.dumps(sample_dict, indent=2)
                full_prompt += schema_instructions

            # --- Pre-flight quota-aware model routing ---
            primary_model: str = self.model or "gemini-3.6-flash"
            try:
                from agent import memory
                quotas: List[Dict[str, Any]] = memory.get_model_quotas()
                gemini_q: Optional[Dict[str, Any]] = next((q for q in quotas if q["model_family"] == "gemini"), None)
                if gemini_q and primary_model.lower().startswith("gemini"):
                    pct_5h: float = gemini_q.get("pct_5h", 100.0)
                    pct_weekly: float = gemini_q.get("pct_weekly", 100.0)
                    if pct_5h < 15.0 or pct_weekly < 15.0:
                        print(f"[QUOTA] Gemini remaining low (5h: {pct_5h:.1f}%, weekly: {pct_weekly:.1f}%). Rerouting to 3P pool.")
                        primary_model = THREE_P_POOL[0]
            except Exception:
                pass  # Quota check is best-effort; don't block on failures

            # --- Build failover sequence based on task priority and pools ---
            failover_sequence: List[str] = []
            if self.roleplay:
                failover_sequence = [primary_model, "ollama/gemma4:12b"]
            else:
                is_primary_gemini: bool = any(primary_model.lower().startswith(g.lower().split("-")[0]) for g in GEMINI_POOL) or primary_model.lower().startswith("gemini")

                if is_primary_gemini:
                    for m in GEMINI_POOL:
                        if m not in failover_sequence:
                            failover_sequence.append(m)
                    if self.task_priority <= TaskPriority.SCHEDULED_CRITICAL:
                        for m in THREE_P_POOL:
                            if m not in failover_sequence:
                                failover_sequence.append(m)
                else:
                    failover_sequence.append(primary_model)
                    for m in GEMINI_POOL:
                        if m not in failover_sequence:
                            failover_sequence.append(m)
                    for m in THREE_P_POOL:
                        if m not in failover_sequence:
                            failover_sequence.append(m)

                if self.task_priority >= TaskPriority.BACKGROUND:
                    failover_sequence = [primary_model]
                elif self.task_priority >= TaskPriority.SCHEDULED_ROUTINE:
                    if is_primary_gemini:
                        failover_sequence = [m for m in failover_sequence if m in GEMINI_POOL]
                    else:
                        failover_sequence = [primary_model]

                # Filter out models with open circuits
                failover_sequence = [m for m in failover_sequence if not _circuit_breaker.is_open(m)]
                if not failover_sequence:
                    failover_sequence = [primary_model]

                # If Grok OAuth is available and it's not a roleplay task, make Grok the first failover
                oauth_file = Path.home() / ".agent" / "xai_oauth.json"
                if oauth_file.exists() and not self.roleplay:
                    grok_model = "grok-build-0.1"
                    if primary_model != grok_model:
                        if grok_model in failover_sequence:
                            failover_sequence.remove(grok_model)
                        failover_sequence.insert(1, grok_model)

            timeout_val: float = self.timeout if self.timeout is not None else 120.0
            last_error: str = "All execution routes failed."

            for current_model in failover_sequence:
                prev_newest: Optional[str] = self._get_newest_conversation_id()
                try:
                    result: Any = await routing_engine.execute(
                        prompt=full_prompt,
                        model=current_model,
                        timeout=self.timeout,
                        conversation_id=self.conversation_id,
                        task_priority=self.task_priority
                    )
                    if result is not None:
                        _circuit_breaker.record_success(current_model)
                        return KeylessAgyResponse(result, timeout_val, agent=self, prev_newest=prev_newest, task_id=task_id)
                except Exception as e:
                    _circuit_breaker.record_failure(current_model)
                    last_error = str(e)
                    print(f"[FAILOVER] Model {current_model} failed: {e}. Trying next model...")
                    
                    # If Grok OAuth failed due to an authorization/permission issue, trigger xai_auth
                    if current_model == "grok-build-0.1":
                        err_msg_lower = last_error.lower()
                        if any(k in err_msg_lower for k in ("oauth", "403", "401", "permission-denied", "token")):
                            print("[FAILOVER] Grok auth failure detected. Spawning non-blocking auth helper...")
                            try:
                                import sys
                                project_root = Path(__file__).parent.parent.parent.parent
                                script_path = project_root / "scratch" / "xai_auth.py"
                                proc = await asyncio.create_subprocess_exec(
                                    sys.executable, str(script_path), "--non-blocking",
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE,
                                    cwd=str(project_root)
                                )
                                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
                                out_str = stdout.decode("utf-8", errors="replace")
                                
                                url = None
                                code = None
                                for line in out_str.splitlines():
                                    if line.startswith("VERIFICATION_URL:"):
                                        url = line.split("VERIFICATION_URL:", 1)[1].strip()
                                    elif line.startswith("USER_CODE:"):
                                        code = line.split("USER_CODE:", 1)[1].strip()
                                        
                                if url and code:
                                    is_discord = self.conversation_id and self.conversation_id.startswith("discord-")
                                    if is_discord:
                                        self.grok_auth_alert = {"url": url, "code": code}
                                    else:
                                        notice = (
                                            f"\n\n[SYSTEM NOTICE: xAI Grok OAuth authentication has failed. "
                                            f"A new authorization request has been generated. "
                                            f"You MUST print this link and code to the user in Discord so they can authorize Grok:\n"
                                            f"Link: {url}\n"
                                            f"Code: {code}]"
                                        )
                                        full_prompt += notice
                            except Exception as auth_err:
                                print(f"[FAILOVER] Failed to run xai_auth: {auth_err}")

            # --- Grok fallback (only for INTERACTIVE and SCHEDULED_CRITICAL priority) ---
            if not self.roleplay and self.task_priority <= TaskPriority.SCHEDULED_CRITICAL:
                # 1. Try Grok OAuth fallback first if credentials exist
                save_file = Path.home() / ".agent" / "xai_oauth.json"
                if save_file.exists():
                    print("[FAILOVER] All models failed. Pivoting to Grok OAuth fallback...")
                    try:
                        g_result = await routing_engine.execute(
                            prompt=full_prompt,
                            model="grok-build-0.1",
                            timeout=self.timeout,
                            conversation_id=self.conversation_id,
                            task_priority=self.task_priority
                        )
                        if g_result is not None and not g_result.error:
                            return KeylessAgyResponse(str(g_result), task_id=task_id)
                        else:
                            last_error = f"Grok OAuth fallback failed: {g_result.error if g_result else 'Unknown error'}"
                    except Exception as e:
                        last_error = f"Grok OAuth fallback failed: {e}"

                # 2. Try Grok CLI binary fallback
                grok_path: Optional[str] = get_grok_path()
                if grok_path:
                    if check_and_record_grok_usage(db_path=self.db_path):
                        print("[FAILOVER] Pivoting to Grok CLI binary fallback...")
                        grok_cmd: List[str] = [grok_path, "-p", full_prompt, "--deny", "*", "--no-plan"]
                        try:
                            proc: Any = await asyncio.create_subprocess_exec(
                                *grok_cmd,
                                stdin=asyncio.subprocess.DEVNULL,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                                cwd=self.cwd
                            )
                            stdout: bytes
                            stderr: bytes
                            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_val)
                            response_text: str = stdout.decode("utf-8", errors="replace")
                            if proc.returncode == 0 and response_text.strip():
                                return KeylessAgyResponse(response_text, task_id=task_id)
                            else:
                                grok_err: str = stderr.decode("utf-8", errors="replace").strip() or "Empty response from grok"
                                last_error = f"Grok fallback failed: {grok_err}"
                        except asyncio.TimeoutError:
                            last_error = f"Grok fallback timed out after {timeout_val} seconds"
                            try:
                                proc.kill()
                                await proc.wait()
                            except Exception:
                                pass
                        except Exception as e:
                            last_error = f"Grok fallback failed: {e}"
                    else:
                        last_error += " (Grok fallback blocked by rate limits)"
                else:
                    last_error += " (Grok binary not found)"

            # --- Direct API fallback (gated behind AGENT_USE_DIRECT_API env flag) ---
            if os.environ.get("AGENT_USE_DIRECT_API", "").lower() == "true":
                direct_result: Optional[str] = await self._call_direct_api(primary_model, full_prompt)
                if direct_result:
                    return KeylessAgyResponse(direct_result, task_id=task_id)

            raise RuntimeError(f"All models in priority failover chain failed. Last error: {last_error}")
        except Exception as e:
            try:
                task_manager.update_active_task_status(task_id, "failed")
            except Exception:
                pass
            raise e
