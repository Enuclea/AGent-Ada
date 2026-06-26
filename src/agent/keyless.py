import os
import shutil
import asyncio
import uuid
import glob
import json
import re
from pathlib import Path
from typing import Optional, List, Dict, Any
from google.antigravity.models import GeminiAPIEndpoint

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
        return agy_path
        
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
                
            try:
                while True:
                    line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=self.timeout_val)
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace")
                    self.stdout_lines.append(decoded)
                    yield decoded
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
        
    for p in ["/Users/dan/.local/bin/grok", "/home/dan/.local/bin/grok"]:
        if os.path.exists(p):
            return p
            
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
    ):
        self.model = model
        self.system_instructions = system_instructions
        self.conversation_id = conversation_id
        self.response_schema = response_schema
        self.db_path = db_path
        self.timeout = timeout
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
            # Generate sample JSON from response_schema fields
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

        harness_path = get_harness_path() or "agy"
        cmd = [harness_path, "-p", full_prompt, "--dangerously-skip-permissions"]
        if self.conversation_id:
            cmd.extend(["--conversation", self.conversation_id])
        if self.model:
            cmd.extend(["--model", self.model])

        timeout_val = self.timeout if self.timeout is not None else 30.0
        prev_newest = self._get_newest_conversation_id()

        # For long user chats (timeout > 30s), stream in real-time immediately
        if timeout_val > 30.0:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            return KeylessAgyResponse(proc, timeout_val, agent=self, prev_newest=prev_newest)

        # For short background tasks (timeout <= 30s), run synchronously with retries and fallback
        max_retries = 2
        backoff = 1.0
        last_error = "Unknown error"

        for attempt in range(max_retries):
            # Get list of existing conversation IDs before run
            prev_newest = self._get_newest_conversation_id()

            try:
                # Run agy command, redirecting stdin from DEVNULL to avoid hangs
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                # Enforce timeout for model response
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_val)
                
                response_text = stdout.decode("utf-8", errors="replace")
                stderr_text = stderr.decode("utf-8", errors="replace")

                if proc.returncode == 0 and response_text.strip():
                    # Look for newly created conversation database
                    curr_newest = self._get_newest_conversation_id()
                    if curr_newest and curr_newest != prev_newest:
                        self.conversation_id = curr_newest
                    elif not self.conversation_id and curr_newest:
                        self.conversation_id = curr_newest

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
                import logging
                logging.getLogger(__name__).warning(
                    "Keyless Gemini/agy call failed (attempt %d/%d): %s. Retrying in %ss...",
                    attempt + 1, max_retries, last_error, backoff
                )
                await asyncio.sleep(backoff)
                backoff *= 2.0

        # Fallback to Grok if available and permitted by guardrails
        grok_path = get_grok_path()
        if grok_path:
            if check_and_record_grok_usage(db_path=self.db_path):
                import logging
                logging.getLogger(__name__).warning("All agy attempts failed. Falling back to grok...")
                grok_cmd = [grok_path, "-p", full_prompt, "--deny", "*", "--no-plan"]
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *grok_cmd,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
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

        raise RuntimeError(f"Keyless Gemini/agy and grok fallback failed. Last error: {last_error}")

