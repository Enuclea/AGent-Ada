import os
import asyncio
from typing import List, Optional, Union
from agent.routes.base import BaseRoute, RouteStatus, get_harness_path

class AgyRoute(BaseRoute):
    @property
    def name(self) -> str:
        return "agy"

    @property
    def default_status(self) -> RouteStatus:
        return RouteStatus.PRIMARY

    @property
    def default_priority(self) -> int:
        return 10

    @property
    def supported_models(self) -> List[str]:
        # Supports Gemini, Claude, and general 3P models accessible via agy
        return ["gemini", "claude", "gpt-4o", "*"]

    async def execute(
        self,
        prompt: str,
        model: str,
        system_instructions: Optional[str] = None,
        timeout: Optional[float] = None,
        conversation_id: Optional[str] = None,
    ) -> Optional[Union[str, asyncio.subprocess.Process]]:
        harness_path = get_harness_path() or "agy"
        
        # Build prompt format
        full_prompt = prompt
        if system_instructions:
            full_prompt = f"[System Instructions]\n{system_instructions}\n\n[User Prompt]\n{prompt}"

        cmd = [harness_path, "-p", full_prompt, "--dangerously-skip-permissions"]
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        cmd.extend(["--model", model])

        # If it is streaming (timeout > 30s), return the subprocess for streaming consumption
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
                print(f"[ROUTE: agy] Failed to spawn streaming subprocess: {e}")
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
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout or 120.0)
                if proc.returncode == 0:
                    return stdout.decode("utf-8", errors="replace").strip()
                else:
                    last_err = stderr.decode("utf-8", errors="replace").strip() or "Empty response"
            except asyncio.TimeoutError:
                last_err = f"Timeout after {timeout or 120.0} seconds"
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

        print(f"[ROUTE: agy] Execution failed after {max_retries} attempts. Last error: {last_err}")
        return None
