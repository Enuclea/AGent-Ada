import os
import asyncio
import aiohttp
import sqlite3
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union
from agent.routes.base import BaseRoute, RouteStatus, RouteInput, RouteOutput
from agent.storage.db import DB_FILE_PATH
from agent.observability.telemetry import log_token_usage

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

def get_cached_credit_usage() -> int:
    now = datetime.now(timezone.utc)
    current_month = f"{now.year:04d}-{now.month:02d}"
    
    conn = sqlite3.connect(str(DB_FILE_PATH))
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM persistent_memory WHERE key = 'onemin_used_credit'")
        row = cursor.fetchone()
        if row:
            data = json.loads(row[0])
            if data.get("month") == current_month:
                return data.get("used_credit", 0)
    except Exception as e:
        print(f"[1MIN] Failed to get cached credit usage: {e}")
    finally:
        conn.close()
    return 0

def save_cached_credit_usage(used_credit: int) -> None:
    now = datetime.now(timezone.utc)
    current_month = f"{now.year:04d}-{now.month:02d}"
    value_str = json.dumps({"used_credit": used_credit, "month": current_month})
    
    conn = sqlite3.connect(str(DB_FILE_PATH))
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO persistent_memory (key, value) VALUES ('onemin_used_credit', ?)",
            (value_str,)
        )
        conn.commit()
    except Exception as e:
        print(f"[1MIN] Failed to save cached credit usage: {e}")
    finally:
        conn.close()

class OneMinCustomRoute(BaseRoute):
    """Custom execution route for 1Min AI.
    
    Enforces a monthly limit of 4M credits, leaving a 1M credit reserve at all times.
    """

    @property
    def name(self) -> str:
        return "onemin"

    @property
    def default_status(self) -> RouteStatus:
        return RouteStatus.ON

    @property
    def default_priority(self) -> int:
        # Runs as a fallback/redundant link after agy, BYOK, and Magica
        return 60

    @property
    def supported_models(self) -> List[str]:
        return ["onemin/", "1min/", "gemini-", "claude-", "gpt-", "*"]

    def map_model(self, model: str) -> str:
        model_lower = model.lower()
        for prefix in ["1min/", "onemin/", "magica/", "byok/"]:
            if model_lower.startswith(prefix):
                model_lower = model_lower[len(prefix):]

        # Map to the latest verified frontier models
        if "claude" in model_lower and "opus" in model_lower:
            return "claude-opus-4-8"
        elif "gemini" in model_lower and "flash" in model_lower:
            return "gemini-3.5-flash"
        elif "gpt" in model_lower and "pro" in model_lower:
            return "gpt-5.5-pro"
        elif "grok" in model_lower and "fast" in model_lower:
            return "grok-4-fast-reasoning"
        elif "opus" in model_lower:
            return "claude-opus-4-8"
        elif "flash" in model_lower:
            return "gemini-3.5-flash"
        elif "pro" in model_lower:
            if "gemini" in model_lower:
                return "gemini-1.5-pro"
            return "gpt-5.5-pro"
        elif "sonnet" in model_lower:
            return "claude-3-5-sonnet"
        elif "gpt-4o-mini" in model_lower:
            return "gpt-4o-mini"
        elif "gpt-4o" in model_lower:
            return "gpt-4o"
        elif "gemini" in model_lower:
            return "gemini-3.5-flash"
        elif "claude" in model_lower:
            return "claude-3-5-sonnet"
        elif "gpt" in model_lower:
            return "gpt-4o-mini"
        
        return model_lower if model_lower not in ["", "*", "default"] else "gemini-3.5-flash"

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
        api_key = os.environ.get("1MIN_AI_API")
        if not api_key:
            err_msg = "1MIN_AI_API key not set."
            print(f"[ROUTE: onemin] Skipped: {err_msg}")
            return RouteOutput(latency=time.time() - start_time, error=err_msg)

        # Check monthly usage (4M credits limit, 1M reserve -> 3M consumable)
        current_credit_usage = get_cached_credit_usage()
        limit = 3_000_000
        if current_credit_usage >= limit:
            err_msg = f"Monthly credit usage limit reached ({current_credit_usage} >= {limit}). Leaving 1M reserve."
            print(f"[ROUTE: onemin] Skipped: {err_msg}")
            return RouteOutput(latency=time.time() - start_time, error=err_msg)

        # Build prompt
        full_prompt = prompt
        if system_instructions:
            full_prompt = f"[System Instructions]\n{system_instructions}\n\n[User Prompt]\n{prompt}"

        target_model = self.map_model(model)
        target_lower = target_model.lower()

        # Check if boardroom
        is_boardroom = False
        if conversation_id:
            if "boardroom" in str(conversation_id).lower():
                is_boardroom = True

        # Check if GPT (GPT 5.5 Pro, etc.) was specifically asked
        is_gpt_specifically_asked = "gpt" in target_lower

        # Map generic/default/wildcard model to preferred cost-efficient model (gemini-3.5-flash)
        if target_model in ("", "*", "default"):
            target_model = "gemini-3.5-flash"
            print(f"[ROUTE: onemin] Default/generic model resolved to preferred failover: {target_model}")

        # Setup candidates for 1Min AI:
        # Grok (136) and Gemini (455) are preferred failovers
        # Opus (2475) is reasonable
        # GPT (18090) is very expensive - avoid unless specifically asked or boardroom
        failovers = ["grok-4-fast-reasoning", "gemini-3.5-flash", "claude-opus-4-8"]
        candidates = [target_model]
        for f in failovers:
            if f not in candidates:
                candidates.append(f)

        url = "https://api.1min.ai/api/chat-with-ai"
        headers = {
            "API-KEY": api_key,
            "Content-Type": "application/json"
        }

        for candidate in candidates:
            # Enforce cost restrictions on GPT: avoid unless in Boardroom or specifically asked
            if "gpt" in candidate.lower():
                if not is_boardroom and not is_gpt_specifically_asked:
                    print(f"[ROUTE: onemin] Skipping expensive GPT route '{candidate}' (non-boardroom and not specifically asked).")
                    continue

            max_retries = 2
            retry_count = 0

            while retry_count <= max_retries:
                print(f"[ROUTE: onemin] Routing request using: {candidate} (attempt {retry_count + 1})")
                payload = {
                    "type": "UNIFY_CHAT_WITH_AI",
                    "model": candidate,
                    "promptObject": {
                        "prompt": full_prompt,
                        "settings": {
                            "historySettings": {
                                "isMixed": False,
                                "historyMessageLimit": 10
                            }
                        }
                    }
                }

                error_type = None  # Can be "quota", "congestion", or "other"

                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, json=payload, headers=headers, timeout=timeout or 60.0) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                
                                # Extract response text
                                ai_record_detail = data.get("aiRecord", {}).get("aiRecordDetail", {})
                                result_object = ai_record_detail.get("resultObject", [])
                                content = result_object[0] if result_object else None
                                
                                if content is not None:
                                    # Extract credit details and update cache
                                    team_user = data.get("aiRecord", {}).get("teamUser", {})
                                    used_credit = team_user.get("usedCredit", 0)
                                    if used_credit > 0:
                                        save_cached_credit_usage(used_credit)

                                    # Log token usage in telemetry
                                    metadata = data.get("aiRecord", {}).get("metadata", {})
                                    input_tokens = metadata.get("inputToken", 0)
                                    output_tokens = metadata.get("outputToken", 0)
                                    cost = metadata.get("credit", 0) / 100000.0  # approximate cost
                                    
                                    log_model_name = f"1min/{candidate}"
                                    active_session = conversation_id or f"1min-{uuid.uuid4()}"
                                    try:
                                        log_token_usage(active_session, log_model_name, input_tokens, output_tokens, cost)
                                    except Exception:
                                        pass
                                    
                                    return RouteOutput(response=content, latency=time.time() - start_time)
                            
                            status_code = resp.status
                            try:
                                resp_body = await resp.text()
                            except Exception:
                                resp_body = ""
                            
                            last_err = f"Model {candidate} returned status {status_code}: {resp_body}"
                            print(f"[ROUTE: onemin] {last_err}")
                            
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
                    print(f"[ROUTE: onemin] Model {candidate} request failed: {e}")
                    
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
                        print(f"[ROUTE: onemin] Quota constraint hit. Retrying the same model {candidate} in {sleep_time:.2f}s...")
                        await asyncio.sleep(sleep_time)
                        continue
                    else:
                        print(f"[ROUTE: onemin] Quota constraint hit. Exceeded retries for {candidate}. Moving to next model.")
                        break
                elif error_type == "congestion":
                    # If we failover due to congestion or lack of response -- we will pointedly use a different model.
                    print(f"[ROUTE: onemin] Congestion or lack of response detected. Pointedly failover to a different model.")
                    break
                else:
                    # For other types of errors, proceed to the next model candidate
                    break

        return RouteOutput(latency=time.time() - start_time, error=last_err)
