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
        return 5

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
        harness_path = get_harness_path() or "agy"
        
        # Build prompt format
        full_prompt = prompt
        if system_instructions:
            full_prompt = f"[System Instructions]\n{system_instructions}\n\n[User Prompt]\n{prompt}"

        # Prepare base harness command without model
        cmd = ["stdbuf", "-oL", "-eL", harness_path, "-p", full_prompt, "--dangerously-skip-permissions"]
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])

        # Prepare unbuffered environment
        sub_env = dict(os.environ)
        sub_env["PYTHONUNBUFFERED"] = "1"
        sub_env["AGENT_RUN_MODE"] = "daemon"
        if conversation_id:
            sub_env["ACTIVE_SESSION_ID"] = conversation_id

        # Determine candidate models to stay within agy route
        model_lower = model.lower()
        if "gemini" in model_lower:
            candidates = ["gemini", "claude"]
        elif "claude" in model_lower:
            candidates = ["claude", "gemini"]
        elif model_lower in ("", "*", "default"):
            candidates = ["gemini", "claude"]
        else:
            candidates = [model, "gemini", "claude"]

        primary_model = candidates[0]

        # If it is streaming (timeout > 30s), return the subprocess immediately for the primary candidate
        if timeout is not None and timeout > 30.0:
            streaming_cmd = list(cmd)
            streaming_cmd.extend(["--model", primary_model])
            try:
                proc = await asyncio.create_subprocess_exec(
                    *streaming_cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=sub_env
                )
                return proc
            except Exception as e:
                print(f"[ROUTE: agy] Failed to spawn streaming subprocess: {e}")
                return None

        # Non-streaming execution with cost/congestion-aware candidate failovers
        last_err = None
        for candidate in candidates:
            candidate_cmd = list(cmd)
            candidate_cmd.extend(["--model", candidate])

            max_retries = 2
            retry_count = 0

            while retry_count <= max_retries:
                print(f"[ROUTE: agy] Attempting execution with model: {candidate} (attempt {retry_count + 1})")
                error_type = None

                try:
                    proc = await asyncio.create_subprocess_exec(
                        *candidate_cmd,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=sub_env
                    )
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout or 120.0)
                    
                    if proc.returncode == 0:
                        return stdout.decode("utf-8", errors="replace").strip()
                    else:
                        last_err = stderr.decode("utf-8", errors="replace").strip() or "Empty response"
                        last_err_lower = last_err.lower()
                        
                        if "quota" in last_err_lower or "rate limit" in last_err_lower or "429" in last_err_lower or "limit exceeded" in last_err_lower:
                            error_type = "quota"
                        elif "timeout" in last_err_lower or "timed out" in last_err_lower or "502" in last_err_lower or "503" in last_err_lower or "504" in last_err_lower or "gateway" in last_err_lower:
                            error_type = "congestion"
                        else:
                            error_type = "other"
                            
                except asyncio.TimeoutError:
                    last_err = f"Timeout after {timeout or 120.0} seconds"
                    error_type = "congestion"
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
                except Exception as e:
                    last_err = str(e)
                    last_err_lower = last_err.lower()
                    if "quota" in last_err_lower or "rate limit" in last_err_lower or "429" in last_err_lower or "limit exceeded" in last_err_lower:
                        error_type = "quota"
                    elif "timeout" in last_err_lower or "timed out" in last_err_lower:
                        error_type = "congestion"
                    else:
                        error_type = "other"

                if error_type == "quota":
                    # If we failover due to quota constraints, the preference is to continue with the same model
                    retry_count += 1
                    if retry_count <= max_retries:
                        sleep_time = 1.5 ** retry_count
                        print(f"[ROUTE: agy] Quota constraint hit for {candidate}. Retrying same model in {sleep_time:.2f}s...")
                        await asyncio.sleep(sleep_time)
                        continue
                    else:
                        print(f"[ROUTE: agy] Quota constraint hit. Exceeded retries for {candidate}. Failing over to next model.")
                        break
                elif error_type == "congestion":
                    # If we failover due to congestion or lack of response -- we will pointedly use a different model
                    print(f"[ROUTE: agy] Congestion or lack of response detected for {candidate}. Pointedly failover to a different model.")
                    break
                else:
                    # For other types of errors, proceed to the next candidate model
                    print(f"[ROUTE: agy] Model {candidate} failed: {last_err}. Moving to next candidate.")
                    break

        print(f"[ROUTE: agy] Execution failed after trying all candidates. Last error: {last_err}")
        return None
