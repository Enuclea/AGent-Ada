import os
import shutil
import asyncio
import uuid
import glob
import json
import re
import time
from enum import IntEnum
from pathlib import Path
from typing import Optional, List, Dict, Any
from google.antigravity.models import GeminiAPIEndpoint


class TaskPriority(IntEnum):
    """Controls failover depth per call type to optimize quota usage."""
    INTERACTIVE = 0       # User is waiting — full failover chain including Grok
    SCHEDULED_CRITICAL = 1  # Grace monitor, Meta-Eval — Gemini + 3P, no Grok
    SCHEDULED_ROUTINE = 2   # Gmail check, Morgen sync — Gemini only, retry next cycle
    BACKGROUND = 3           # Compaction, observer — cheapest model, no failover


class CircuitBreaker:
    """Tracks consecutive failures per model and skips models in 'open' state.
    
    After `failure_threshold` consecutive failures, the circuit opens for
    `reset_seconds` seconds, during which the model is skipped.
    """
    def __init__(self, failure_threshold: int = 3, reset_seconds: float = 300.0):
        self._failures: Dict[str, int] = {}
        self._open_until: Dict[str, float] = {}
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds

    def record_failure(self, model: str) -> None:
        self._failures[model] = self._failures.get(model, 0) + 1
        if self._failures[model] >= self._failure_threshold:
            self._open_until[model] = time.monotonic() + self._reset_seconds

    def record_success(self, model: str) -> None:
        self._failures.pop(model, None)
        self._open_until.pop(model, None)

    def is_open(self, model: str) -> bool:
        """Returns True if the model circuit is open (should be skipped)."""
        deadline = self._open_until.get(model)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            # Circuit has reset — allow retry
            self._open_until.pop(model, None)
            self._failures.pop(model, None)
            return False
        return True


# Module-level circuit breaker instance shared across all agents
_circuit_breaker = CircuitBreaker(failure_threshold=3, reset_seconds=300.0)

# Failover pools — Gemini models share one quota bucket, 3P models share another
GEMINI_POOL = ["gemini-3.5-flash", "gemini-3.5-pro"]
THREE_P_POOL = ["Claude Sonnet 4.6 (Thinking)", "gpt-4o"]

class KeylessGeminiAPIEndpoint(GeminiAPIEndpoint):
    def validate_endpoint(self) -> None:
        # Bypass client-side API key validation for Gemini Developer API.
        # This allows routing to keyless / Ultra plan gateways via Go localharness/agy.
        pass

def get_harness_path() -> Optional[str]:
    """Resolves the path to the system-wide agy binary."""
    if "ANTIGRAVITY_HARNESS_PATH" in os.environ:
        return os.environ["ANTIGRAVITY_HARNESS_PATH"]
    
    # Check system PATH for 'agy'
    agy_path = shutil.which("agy")
    if agy_path:
        try:
            cwd_path = Path.cwd().resolve()
            resolved_path = Path(agy_path).resolve()
            
            is_in_cwd = cwd_path in resolved_path.parents or resolved_path == cwd_path
            
            trusted_dirs = [
                Path("/usr/bin").resolve(),
                Path("/usr/local/bin").resolve(),
                Path("/bin").resolve(),
                Path("/sbin").resolve(),
                Path("~/.local/bin").expanduser().resolve(),
                Path("~/.gemini/antigravity-cli/bin").expanduser().resolve(),
            ]
            is_trusted_parent = resolved_path.parent in trusted_dirs
            
            if is_in_cwd or not is_trusted_parent:
                import logging
                logging.warning(
                    f"agy binary path {agy_path} (resolved to {resolved_path}) failed security check. "
                    f"is_in_cwd={is_in_cwd}, is_trusted_parent={is_trusted_parent}."
                )
            else:
                return agy_path
        except Exception as e:
            import logging
            logging.warning(f"Error resolving agy path: {e}")
        
    # Fallback to standard local bin paths
    user_home = Path.home()
    fallback_path = user_home / ".local" / "bin" / "agy"
    if fallback_path.exists() and fallback_path.is_file():
        return str(fallback_path)
            
    return None

def setup_keyless_environment() -> None:
    """Sets the ANTIGRAVITY_HARNESS_PATH env var if system agy is found."""
    harness_path = get_harness_path()
    if harness_path:
        os.environ["ANTIGRAVITY_HARNESS_PATH"] = harness_path

class KeylessAgyResponse:
    def __init__(self, text_or_proc, timeout_val=None, agent=None, prev_newest=None):
        import asyncio
        if isinstance(text_or_proc, str):
            self.text = text_or_proc
            self._chunks = [text_or_proc]
            self.proc = None
            self.timeout_val = None
            self.agent = None
            self.prev_newest = None
            self.stdout_lines = [text_or_proc]
            self.stderr_lines = []
            self._completed_event = asyncio.Event()
            self._completed_event.set()
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
        self._index = 0

    async def _consume_stream(self):
        if self._completed_event.is_set():
            return
        try:
            while True:
                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=self.timeout_val)
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace")
                self.stdout_lines.append(decoded)
        except asyncio.TimeoutError:
            try:
                self.proc.kill()
                await self.proc.wait()
            except Exception:
                pass
            self.stdout_lines.append(f"\n[Timeout after {self.timeout_val} seconds]\n")
        except Exception as e:
            self.stdout_lines.append(f"\n[Error: {e}]\n")
            
        try:
            stderr_data = await self.proc.stderr.read()
            if stderr_data:
                self.stderr_lines.append(stderr_data.decode("utf-8", errors="replace"))
        except Exception:
            pass
            
        try:
            await self.proc.wait()
        except Exception:
            pass

        if self.agent:
            curr_newest = self.agent._get_newest_conversation_id()
            if curr_newest and curr_newest != self.prev_newest:
                self.agent.conversation_id = curr_newest
            elif not self.agent.conversation_id and curr_newest:
                self.agent.conversation_id = curr_newest

        self.text = "".join(self.stdout_lines)
        self._completed_event.set()

    @property
    def thoughts(self):
        async def _stream_thoughts():
            if self._completed_event.is_set():
                for line in self.stdout_lines:
                    yield line
                return
                
            start_time = asyncio.get_event_loop().time()
            max_duration = self.timeout_val
            try:
                while True:
                    # Calculate remaining time for the overall timeout
                    elapsed = asyncio.get_event_loop().time() - start_time
                    remaining = max_duration - elapsed
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                        
                    # Use a short timeout of 15 seconds to yield keep-alive pings
                    chunk_timeout = min(15.0, remaining)
                    try:
                        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=chunk_timeout)
                        if not line:
                            break
                        decoded = line.decode("utf-8", errors="replace")
                        self.stdout_lines.append(decoded)
                        yield decoded
                    except asyncio.TimeoutError:
                        # Yield a blank keep-alive chunk to reset connection inactivity timers
                        if self.proc.returncode is None:
                            yield ""
                        else:
                            break
            except asyncio.TimeoutError:
                try:
                    self.proc.kill()
                    await self.proc.wait()
                except Exception:
                    pass
                err_msg = f"\n[Timeout after {self.timeout_val} seconds]\n"
                self.stdout_lines.append(err_msg)
                yield err_msg
            except Exception as e:
                yield f"\n[Error: {e}]\n"

            try:
                stderr_data = await self.proc.stderr.read()
                if stderr_data:
                    self.stderr_lines.append(stderr_data.decode("utf-8", errors="replace"))
            except Exception:
                pass

            try:
                await self.proc.wait()
            except Exception:
                pass

            if self.agent:
                curr_newest = self.agent._get_newest_conversation_id()
                if curr_newest and curr_newest != self.prev_newest:
                    self.agent.conversation_id = curr_newest
                elif not self.agent.conversation_id and curr_newest:
                    self.agent.conversation_id = curr_newest
                    
            self.text = "".join(self.stdout_lines)
            self._completed_event.set()

        return _stream_thoughts()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._index >= 1:
            raise StopAsyncIteration
        self._index += 1
        
        await self._consume_stream()
        
        stderr_text = "".join(self.stderr_lines).strip()
        if self.proc and self.proc.returncode != 0 and stderr_text:
            return self.text + f"\n\n[Process exited with code {self.proc.returncode}]\nError output:\n{stderr_text}"
            
        return self.text or "Execution completed."

    async def structured_output(self) -> dict:
        await self._consume_stream()
        text = self.text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        
        pattern = r"```(?:json)?\s*(\{.*?\})\s*```"
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
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
    """Resolves the path to the system-wide grok binary."""
    grok_path = shutil.which("grok")
    if grok_path:
        return grok_path
        
    user_home = Path.home()
    fallback_path = user_home / ".local" / "bin" / "grok"
    if fallback_path.exists() and fallback_path.is_file():
        return str(fallback_path)
            
    return None

def check_and_record_grok_usage(db_path: Optional[str] = None) -> bool:
    """
    Checks if grok fallback usage is within limits (max 5 calls per hour, max 20 per day).
    Returns True if allowed (and logs the call), False otherwise.
    """
    import json
    import time
    from pathlib import Path
    
    path = Path.home() / ".agent" / "grok_fallback_calls.json"
    try:
        if path.exists():
            with open(path, "r") as f:
                calls = json.load(f)
        else:
            calls = []
    except Exception:
        calls = []
        
    now = time.time()
    one_day_ago = now - 86400
    one_hour_ago = now - 3600
    
    # Clean up older timestamps
    calls = [t for t in calls if t > one_day_ago]
    
    hour_calls = [t for t in calls if t > one_hour_ago]
    
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
    ):
        self.model = model
        self.system_instructions = system_instructions
        self.conversation_id = conversation_id
        self.response_schema = response_schema
        self.db_path = db_path
        self.timeout = timeout
        self.task_priority = task_priority
        self.cwd = cwd
        self.roleplay = roleplay
        self._conversations_dir = str(Path.home() / ".gemini" / "antigravity-cli" / "conversations")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def _get_newest_conversation_id(self) -> Optional[str]:
        if not os.path.exists(self._conversations_dir):
            return None
        db_files = glob.glob(os.path.join(self._conversations_dir, "*.db"))
        if not db_files:
            return None
        db_files.sort(key=os.path.getmtime, reverse=True)
        return os.path.basename(db_files[0])[:-3]

    async def _call_direct_api(self, model_name: str, full_prompt: str) -> Optional[str]:
        """Attempts to call direct provider APIs if API keys are found in environment."""
        import aiohttp
        
        # 1. Gemini direct API
        gemini_key = os.environ.get("GEMINI_API_KEY")
        if gemini_key and ("gemini" in model_name.lower() or model_name == "default"):
            actual_model = model_name if "gemini" in model_name.lower() else "gemini-1.5-flash"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{actual_model}:generateContent?key={gemini_key}"
            headers = {"Content-Type": "application/json"}
            payload = {"contents": [{"parts": [{"text": full_prompt}]}]}
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=headers, json=payload, timeout=self.timeout or 30.0) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data["candidates"][0]["content"]["parts"][0]["text"]
            except Exception as e:
                print(f"[DIRECT-API] Gemini API call failed: {e}")

        # 2. Anthropic direct API
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
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
                "messages": [{"role": "user", "content": full_prompt}]
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=headers, json=payload, timeout=self.timeout or 30.0) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data["content"][0]["text"]
            except Exception as e:
                print(f"[DIRECT-API] Anthropic API call failed: {e}")

        # 3. OpenAI direct API
        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key and ("gpt" in model_name.lower() or "openai" in model_name.lower()):
            actual_model = "gpt-4o" if "gpt" in model_name.lower() else model_name
            url = "https://api.openai.com/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": actual_model,
                "messages": [{"role": "user", "content": full_prompt}]
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=headers, json=payload, timeout=self.timeout or 30.0) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data["choices"][0]["message"]["content"]
            except Exception as e:
                print(f"[DIRECT-API] OpenAI API call failed: {e}")

        # 4. Ollama local API
        ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        if "ollama" in model_name.lower() or os.environ.get("USE_OLLAMA") == "true":
            actual_model = model_name.replace("ollama/", "") if "/" in model_name else "llama3"
            url = f"{ollama_host}/api/generate"
            payload = {
                "model": actual_model,
                "prompt": full_prompt,
                "stream": False
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=self.timeout or 60.0) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data["response"]
            except Exception as e:
                print(f"[LOCAL-OLLAMA] Ollama call failed: {e}")
                
        return None

    async def chat(self, prompt: str) -> KeylessAgyResponse:
        full_prompt = prompt
        if self.system_instructions:
            full_prompt = f"[System Instructions]\n{self.system_instructions}\n\n[User Prompt]\n{prompt}"

        # If response_schema is specified, request JSON format
        if self.response_schema:
            schema_instructions = (
                "\n\nYou MUST return the response ONLY as a raw JSON object. Do not include markdown code block formatting (such as ```json). "
                "Do not include any explanation or extra text. The JSON object structure MUST match:\n"
            )
            if hasattr(self.response_schema, "model_fields"):
                schema_fields = self.response_schema.model_fields
            elif hasattr(self.response_schema, "__fields__"):
                schema_fields = self.response_schema.__fields__
            else:
                schema_fields = {}
            
            sample_dict = {}
            for name, field in schema_fields.items():
                annotation = getattr(field, "annotation", str)
                if annotation is bool:
                    sample_dict[name] = True
                elif annotation is int:
                    sample_dict[name] = 0
                else:
                    sample_dict[name] = "string"
            schema_instructions += json.dumps(sample_dict, indent=2)
            full_prompt += schema_instructions

        # --- Pre-flight quota-aware model routing ---
        primary_model = self.model or "gemini-3.5-flash"
        try:
            from agent import memory
            quotas = memory.get_model_quotas()
            gemini_q = next((q for q in quotas if q["model_family"] == "gemini"), None)
            if gemini_q and primary_model.lower().startswith("gemini"):
                pct_5h = gemini_q.get("pct_5h", 100.0)
                pct_weekly = gemini_q.get("pct_weekly", 100.0)
                if pct_5h < 15.0 or pct_weekly < 15.0:
                    print(f"[QUOTA] Gemini remaining low (5h: {pct_5h:.1f}%, weekly: {pct_weekly:.1f}%). Rerouting to 3P pool.")
                    primary_model = THREE_P_POOL[0]
        except Exception:
            pass  # Quota check is best-effort; don't block on failures

        # --- Build failover sequence based on task priority and pools ---
        failover_sequence = []
        if self.roleplay:
            failover_sequence = [primary_model, "ollama/gemma4:12b"]
        else:
            # Determine which pool the primary model belongs to
            is_primary_gemini = any(primary_model.lower().startswith(g.lower().split("-")[0]) for g in GEMINI_POOL) or primary_model.lower().startswith("gemini")

            if is_primary_gemini:
                # Start with Gemini pool, then fall to 3P pool
                for m in GEMINI_POOL:
                    if m not in failover_sequence:
                        failover_sequence.append(m)
                if self.task_priority <= TaskPriority.SCHEDULED_CRITICAL:
                    for m in THREE_P_POOL:
                        if m not in failover_sequence:
                            failover_sequence.append(m)
            else:
                # Primary is 3P — try it first, then Gemini, then rest of 3P
                failover_sequence.append(primary_model)
                for m in GEMINI_POOL:
                    if m not in failover_sequence:
                        failover_sequence.append(m)
                for m in THREE_P_POOL:
                    if m not in failover_sequence:
                        failover_sequence.append(m)

            # For BACKGROUND priority, only try the primary model — no failover
            if self.task_priority >= TaskPriority.BACKGROUND:
                failover_sequence = [primary_model]
            # For SCHEDULED_ROUTINE, only try models in the primary pool
            elif self.task_priority >= TaskPriority.SCHEDULED_ROUTINE:
                if is_primary_gemini:
                    failover_sequence = [m for m in failover_sequence if m in GEMINI_POOL]
                else:
                    failover_sequence = [primary_model]

            # Filter out models with open circuits
            failover_sequence = [m for m in failover_sequence if not _circuit_breaker.is_open(m)]
            if not failover_sequence:
                # All circuits open — force-try primary model anyway
                failover_sequence = [primary_model]

        timeout_val = self.timeout if self.timeout is not None else 30.0
        harness_path = get_harness_path() or "agy"
        last_error = "All agy execution attempts failed."

        for current_model in failover_sequence:
            if current_model.startswith("ollama/"):
                import aiohttp
                url = "http://10.200.0.4:11434/api/generate"
                payload = {
                    "model": current_model.replace("ollama/", ""),
                    "prompt": full_prompt,
                    "stream": False
                }
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, json=payload, timeout=self.timeout or 60.0) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                return KeylessAgyResponse(data["response"])
                            else:
                                raise RuntimeError(f"Ollama Mac Mini returned status {resp.status}")
                except Exception as e:
                    print(f"[FAILOVER] Ollama Mac Mini call failed: {e}")
                    continue

            cmd = [harness_path, "-p", full_prompt, "--dangerously-skip-permissions"]
            if self.conversation_id:
                cmd.extend(["--conversation", self.conversation_id])
            cmd.extend(["--model", current_model])

            # For long-running user chats (timeout > 30s), run primary model only with streaming
            if timeout_val > 30.0:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=self.cwd
                    )
                    _circuit_breaker.record_success(current_model)
                    return KeylessAgyResponse(proc, timeout_val, agent=self, prev_newest=self._get_newest_conversation_id())
                except Exception as e:
                    _circuit_breaker.record_failure(current_model)
                    last_error = str(e)
                    # If primary streaming fails, continue to next model in failover sequence
                    continue

            # Run with retries for short/background tasks (timeout <= 30s)
            max_retries = 2
            backoff = 1.0

            for attempt in range(max_retries):
                prev_newest = self._get_newest_conversation_id()
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=self.cwd
                    )
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_val)
                    response_text = stdout.decode("utf-8", errors="replace")
                    stderr_text = stderr.decode("utf-8", errors="replace")

                    if proc.returncode == 0 and response_text.strip():
                        curr_newest = self._get_newest_conversation_id()
                        if curr_newest and curr_newest != prev_newest:
                            self.conversation_id = curr_newest
                        elif not self.conversation_id and curr_newest:
                            self.conversation_id = curr_newest

                        _circuit_breaker.record_success(current_model)
                        return KeylessAgyResponse(response_text)
                    else:
                        last_error = stderr_text.strip() or response_text.strip() or "Empty response"
                except asyncio.TimeoutError:
                    last_error = f"Timeout after {timeout_val} seconds"
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
                except Exception as e:
                    last_error = str(e)

                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2.0

            # If we reached here, the current model in the sequence failed. Try next model.
            _circuit_breaker.record_failure(current_model)
            print(f"[FAILOVER] Model {current_model} failed. Trying next model...")

        # --- Grok fallback (only for INTERACTIVE and SCHEDULED_CRITICAL priority) ---
        if not self.roleplay and self.task_priority <= TaskPriority.SCHEDULED_CRITICAL:
            grok_path = get_grok_path()
            if grok_path:
                if check_and_record_grok_usage(db_path=self.db_path):
                    print("[FAILOVER] All models failed. Pivoting to Grok fallback...")
                    grok_cmd = [grok_path, "-p", full_prompt, "--deny", "*", "--no-plan"]
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            *grok_cmd,
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=self.cwd
                        )
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_val)
                        response_text = stdout.decode("utf-8", errors="replace")
                        if proc.returncode == 0 and response_text.strip():
                            return KeylessAgyResponse(response_text)
                        else:
                            grok_err = stderr.decode("utf-8", errors="replace").strip() or "Empty response from grok"
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
            direct_result = await self._call_direct_api(primary_model, full_prompt)
            if direct_result:
                return KeylessAgyResponse(direct_result)

        raise RuntimeError(f"All models in priority failover chain failed. Last error: {last_error}")


