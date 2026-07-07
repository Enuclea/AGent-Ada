import os
import asyncio
import aiohttp
from pathlib import Path
from typing import List, Optional, Union
from agent.routes.base import BaseRoute, RouteStatus, RouteInput, RouteOutput

def load_env_keys() -> None:
    env_path = Path.home() / ".env"
    if env_path.exists():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip()
                        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                            v = v[1:-1]
                        if k and v and k not in os.environ:
                            os.environ[k] = v
        except Exception as e:
            print(f"[ENV] Failed to load keys from {env_path}: {e}")

class MagicaCustomRoute(BaseRoute):
    """Custom execution route for Magica.
    
    Routes based on model cost profiles:
    - Preferred failovers: Gemini 3.5 Flash (0.0003M) and Grok 4.3 (0.0003M)
    - Reasonable fallback: Claude Opus 4.8 (0.0005M)
    - Avoid expensive DeepSeek V3.2 (0.0054M) unless in Boardroom or specifically requested.
    """

    @property
    def name(self) -> str:
        return "magica"

    @property
    def default_status(self) -> RouteStatus:
        return RouteStatus.ON

    @property
    def default_priority(self) -> int:
        return 50

    @property
    def supported_models(self) -> List[str]:
        return ["magica/", "claude-3-5", "*"]

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

        import sys
        if "pytest" in sys.modules:
            return RouteOutput(latency=0.0, error="Bypassed during pytest")

        load_env_keys()
        api_key = os.environ.get("MAGICA_API") or os.environ.get("MAGICA_API_KEY")
        if not api_key:
            err_msg = "MAGICA_API key not set."
            print(f"[ROUTE: magica] Skipped: {err_msg}")
            return RouteOutput(latency=time.time() - start_time, error=err_msg)

        # Build prompt format
        full_prompt = prompt
        if system_instructions:
            full_prompt = f"[System Instructions]\n{system_instructions}\n\n[User Prompt]\n{prompt}"

        # Clean model name mapping
        target_model = model.replace("magica/", "")
        target_lower = target_model.lower()

        # Check if this is a Boardroom discussion
        is_boardroom = False
        if conversation_id:
            if "boardroom" in str(conversation_id).lower():
                is_boardroom = True

        # Check if DeepSeek was specifically requested
        is_deepseek_specifically_asked = "deepseek" in target_lower

        # Map generic/default/wildcard model to preferred cost-efficient model
        if target_model in ("", "*", "default"):
            target_model = "gemini-3.5-flash"
            print(f"[ROUTE: magica] Default/generic model resolved to preferred failover: {target_model}")

        # Setup cost-aware candidate list with failovers.
        # We avoid DeepSeek in standard failover chains.
        failovers = ["gemini-3.5-flash", "grok-4.3", "claude-opus-4-8"]
        candidates = [target_model]
        for f in failovers:
            if f not in candidates:
                candidates.append(f)

        url = "https://api.magica.ai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        last_err = "No candidates succeeded"
        for candidate in candidates:
            # Enforce cost restrictions on DeepSeek: avoid unless in Boardroom or specifically asked
            if "deepseek" in candidate.lower():
                if not is_boardroom and not is_deepseek_specifically_asked:
                    print(f"[ROUTE: magica] Skipping expensive DeepSeek route '{candidate}' (non-boardroom and not specifically asked).")
                    continue

            max_retries = 2
            retry_count = 0

            while retry_count <= max_retries:
                print(f"[ROUTE: magica] Routing request using: {candidate} (attempt {retry_count + 1})")
                payload = {
                    "model": candidate,
                    "messages": [
                        {"role": "user", "content": full_prompt}
                    ],
                    "temperature": 0.2
                }

                error_type = None  # Can be "quota", "congestion", or "other"

                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, json=payload, headers=headers, timeout=timeout or 60.0) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                res_str = data["choices"][0]["message"]["content"]
                                return RouteOutput(response=res_str, latency=time.time() - start_time)
                            
                            status_code = resp.status
                            try:
                                resp_body = await resp.text()
                            except Exception:
                                resp_body = ""
                            
                            last_err = f"Model {candidate} returned status {status_code}: {resp_body}"
                            print(f"[ROUTE: magica] {last_err}")
                            
                            body_lower = resp_body.lower()
                            if status_code == 429 or "quota" in body_lower or "rate limit" in body_lower or "limit exceeded" in body_lower:
                                error_type = "quota"
                            elif status_code in (502, 503, 504) or "timeout" in body_lower or "gateway" in body_lower:
                                error_type = "congestion"
                            else:
                                error_type = "other"
                                
                except Exception as e:
                    last_err = str(e)
                    err_msg = str(e).lower()
                    print(f"[ROUTE: magica] Model {candidate} request failed: {e}")
                    
                    if isinstance(e, (asyncio.TimeoutError, aiohttp.ServerTimeoutError, aiohttp.ClientConnectorError)) or "timeout" in err_msg or "timed out" in err_msg:
                        error_type = "congestion"
                    elif "quota" in err_msg or "rate limit" in err_msg or "429" in err_msg or "limit exceeded" in err_msg:
                        error_type = "quota"
                    else:
                        error_type = "other"

                if error_type == "quota":
                    # If we failover due to quota constraints, the preference is to continue with the same model.
                    retry_count += 1
                    if retry_count <= max_retries:
                        sleep_time = 1.5 ** retry_count
                        print(f"[ROUTE: magica] Quota constraint hit. Retrying the same model {candidate} in {sleep_time:.2f}s...")
                        await asyncio.sleep(sleep_time)
                        continue
                    else:
                        print(f"[ROUTE: magica] Quota constraint hit. Exceeded retries for {candidate}. Moving to next model.")
                        break
                elif error_type == "congestion":
                    # If we failover due to congestion or lack of response -- we will pointedly use a different model.
                    print(f"[ROUTE: magica] Congestion or lack of response detected. Pointedly failover to a different model.")
                    break
                else:
                    # For other types of errors, proceed to the next model candidate
                    break

        return RouteOutput(latency=time.time() - start_time, error=last_err)
