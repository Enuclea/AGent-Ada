import os
import shutil
import asyncio
from typing import List, Optional, Union
from agent.routes.base import BaseRoute, RouteStatus

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
        prompt: str,
        model: str,
        system_instructions: Optional[str] = None,
        timeout: Optional[float] = None,
        conversation_id: Optional[str] = None,
    ) -> Optional[Union[str, asyncio.subprocess.Process]]:
        harness_path = shutil.which("grok")
        if not harness_path:
            # Fallback to local user path
            fallback = os.path.expanduser("~/.local/bin/grok")
            if os.path.exists(fallback):
                harness_path = fallback
            else:
                print("[ROUTE: grok] Grok CLI binary not found.")
                return None

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
                return proc
            except Exception as e:
                print(f"[ROUTE: grok] Failed to spawn streaming subprocess: {e}")
                return None

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
                    return stdout.decode("utf-8", errors="replace").strip()
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
        return None
