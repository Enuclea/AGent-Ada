import os
import asyncio
import time
from typing import List, Optional, Union
from agent.routes.base import BaseRoute, RouteStatus, get_harness_path, RouteInput, RouteOutput

class AgyRoute(BaseRoute):
    @property
    def name(self) -> str:
        return "agy"

    @property
    def supports_tools(self) -> bool:
        return True

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

    def supports_model(self, model: str) -> bool:
        if "grok" in model.lower():
            return False
        return super().supports_model(model)

    async def execute(
        self,
        input_data: Union[RouteInput, str] = None,
        model: Optional[str] = None,
        system_instructions: Optional[str] = None,
        timeout: Optional[float] = None,
        conversation_id: Optional[str] = None,
        **kwargs
    ) -> RouteOutput:
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

        def resolve_model_name(m: str) -> str:
            m_lower = m.lower() if m else ""
            if m_lower in ("gemini-3.6-flash", "gemini", "default", ""):
                return "Gemini 3.6 Flash (High)"
            if m_lower == "gemini-3.5-flash":
                return "Gemini 3.5 Flash (Medium)"
            if m_lower in ("claude", "claude-sonnet", "sonnet"):
                return "Claude Sonnet 4.6 (Thinking)"
            return m

        if "gemini" in model_lower:
            candidates = [resolve_model_name(model), "Claude Sonnet 4.6 (Thinking)"]
        elif "claude" in model_lower:
            candidates = [resolve_model_name(model), "Gemini 3.6 Flash (High)"]
        elif model_lower in ("", "*", "default"):
            candidates = ["Gemini 3.6 Flash (High)", "Claude Sonnet 4.6 (Thinking)"]
        else:
            candidates = [resolve_model_name(model), "Gemini 3.6 Flash (High)", "Claude Sonnet 4.6 (Thinking)"]

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
                return RouteOutput(response=proc, latency=time.time() - start_time)
            except Exception as e:
                err_msg = f"Failed to spawn streaming subprocess: {e}"
                print(f"[ROUTE: agy] {err_msg}")
                return RouteOutput(latency=time.time() - start_time, error=err_msg)

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
                        res_str = stdout.decode("utf-8", errors="replace").strip()
                        return RouteOutput(response=res_str, latency=time.time() - start_time)
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
        return RouteOutput(latency=time.time() - start_time, error=last_err)
