import os
import shutil
import asyncio
from typing import List, Optional, Union
from agent.routes.base import BaseRoute, RouteStatus, RouteInput, RouteOutput

class GrokRoute(BaseRoute):
    @property
    def name(self) -> str:
        return "grok"

    @property
    def default_status(self) -> RouteStatus:
        return RouteStatus.SECONDARY

    @property
    def default_priority(self) -> int:
        return 20

    @property
    def supported_models(self) -> List[str]:
        # Grok route handles grok models natively
        return ["grok"]

    async def execute(
        self,
        input_data: Union[RouteInput, str] = None,
        model: Optional[str] = None,
        system_instructions: Optional[str] = None,
        timeout: Optional[float] = None,
        conversation_id: Optional[str] = None,
        **kwargs
    ) -> RouteOutput:
        import time
        start_time = time.time()
        if isinstance(input_data, RouteInput):
            prompt = input_data.prompt
            model = input_data.model
            system_instructions = input_data.system_instructions
            timeout = input_data.timeout
            conversation_id = input_data.conversation_id
        else:
            prompt = input_data if isinstance(input_data, str) else kwargs.get("prompt", "")
            model = model or "*"

        import re
        if model.startswith("-"):
            raise ValueError("model cannot start with a hyphen")
        if not re.match(r"^[a-zA-Z0-9_\-\./\*]+$", model):
            raise ValueError(f"Invalid model: {model}")
        if conversation_id is not None:
            if conversation_id.startswith("-"):
                raise ValueError("conversation_id cannot start with a hyphen")
            if not re.match(r"^[a-zA-Z0-9_\-\.:]+$", conversation_id):
                raise ValueError(f"Invalid conversation_id: {conversation_id}")
        harness_path = shutil.which("grok")
        if not harness_path:
            # Fallback to local user path
            fallback = os.path.expanduser("~/.local/bin/grok")
            if os.path.exists(fallback):
                harness_path = fallback
            else:
                err_msg = "Grok CLI binary not found."
                print(f"[ROUTE: grok] {err_msg}")
                return RouteOutput(latency=time.time() - start_time, error=err_msg)

        # Build prompt
        full_prompt = prompt
        if system_instructions:
            full_prompt = f"[System Instructions]\n{system_instructions}\n\n[User Prompt]\n{prompt}"

        cmd = [harness_path, "-p", full_prompt, "--dangerously-skip-permissions"]
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        cmd.extend(["--model", model])

        if timeout is not None and timeout > 30.0:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                return RouteOutput(response=proc, latency=time.time() - start_time)
            except Exception as e:
                err_msg = f"Failed to spawn streaming subprocess: {e}"
                print(f"[ROUTE: grok] {err_msg}")
                return RouteOutput(latency=time.time() - start_time, error=err_msg)

        # Non-streaming execution with retries
        max_retries = 2
        backoff = 1.0
        last_err = None

        for attempt in range(max_retries):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout or 30.0)
                if proc.returncode == 0:
                    res_str = stdout.decode("utf-8", errors="replace").strip()
                    return RouteOutput(response=res_str, latency=time.time() - start_time)
                else:
                    last_err = stderr.decode("utf-8", errors="replace").strip() or "Empty response"
            except asyncio.TimeoutError:
                last_err = f"Timeout after {timeout or 30.0} seconds"
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            except Exception as e:
                last_err = str(e)

            if attempt < max_retries - 1:
                await asyncio.sleep(backoff)
                backoff *= 2.0

        print(f"[ROUTE: grok] Execution failed after {max_retries} attempts. Last error: {last_err}")
        return RouteOutput(latency=time.time() - start_time, error=last_err)
