import os
import json
import aiohttp
from pathlib import Path
from typing import List, Optional, Union
from agent.routes.base import BaseRoute, RouteStatus, RouteInput, RouteOutput

def load_api_keys() -> None:
    config_path = Path("config/api_keys.json")
    if not config_path.exists():
        home_config = Path.home() / ".agent" / "api_keys.json"
        if home_config.exists():
            config_path = home_config
        else:
            return

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in data.items():
                if v and k not in os.environ and not str(v).endswith("-here"):
                    os.environ[k] = str(v)
    except Exception as e:
        print(f"[ROUTE: byok] Failed to load keys from {config_path}: {e}")

class BYOKRoute(BaseRoute):
    @property
    def name(self) -> str:
        return "byok"

    @property
    def default_status(self) -> RouteStatus:
        return RouteStatus.PRIMARY

    @property
    def default_priority(self) -> int:
        # Standard API fallback priority
        return 10

    @property
    def supported_models(self) -> List[str]:
        # Models supported by direct APIs
        return ["gemini", "claude", "sonnet", "gpt"]

    def supports_model(self, model: str) -> bool:
        load_api_keys()
        model_lower = model.lower()
        if "gemini" in model_lower:
            return bool(os.environ.get("GEMINI_API_KEY"))
        if "claude" in model_lower or "sonnet" in model_lower:
            return bool(os.environ.get("ANTHROPIC_API_KEY"))
        if "gpt" in model_lower:
            return bool(os.environ.get("OPENAI_API_KEY"))
        if model_lower == "default":
            return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))
        return False

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

        load_api_keys()
        model_lower = model.lower()
        full_prompt = prompt
        if system_instructions:
            full_prompt = f"[System Instructions]\n{system_instructions}\n\n[User Prompt]\n{prompt}"

        # 1. Gemini
        gemini_key = os.environ.get("GEMINI_API_KEY")
        if gemini_key and ("gemini" in model_lower or model_lower == "default"):
            actual_model = model if "gemini" in model_lower else "gemini-1.5-flash"
            if "3.5" in actual_model or "2.5" in actual_model:
                if "pro" in actual_model:
                    actual_model = "gemini-1.5-pro"
                else:
                    actual_model = "gemini-1.5-flash"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{actual_model}:generateContent?key={gemini_key}"
            headers = {"Content-Type": "application/json"}
            payload = {"contents": [{"parts": [{"text": full_prompt}]}]}
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=headers, json=payload, timeout=timeout or 30.0) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            res_str = data["candidates"][0]["content"]["parts"][0]["text"]
                            return RouteOutput(response=res_str, latency=time.time() - start_time)
                        else:
                            err_msg = f"Gemini API returned status {resp.status}"
                            print(f"[ROUTE: byok] {err_msg}")
                            return RouteOutput(latency=time.time() - start_time, error=err_msg)
            except Exception as e:
                err_msg = str(e)
                print(f"[ROUTE: byok] Gemini API call failed: {e}")
                return RouteOutput(latency=time.time() - start_time, error=err_msg)

        # 2. Anthropic / Claude
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        if anthropic_key and ("claude" in model_lower or "sonnet" in model_lower):
            actual_model = "claude-3-5-sonnet-20241022" if "sonnet" in model_lower else model
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
                    async with session.post(url, headers=headers, json=payload, timeout=timeout or 30.0) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            res_str = data["content"][0]["text"]
                            return RouteOutput(response=res_str, latency=time.time() - start_time)
                        else:
                            err_msg = f"Anthropic API returned status {resp.status}"
                            print(f"[ROUTE: byok] {err_msg}")
                            return RouteOutput(latency=time.time() - start_time, error=err_msg)
            except Exception as e:
                err_msg = str(e)
                print(f"[ROUTE: byok] Anthropic API call failed: {e}")
                return RouteOutput(latency=time.time() - start_time, error=err_msg)

        # 3. OpenAI / GPT
        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key and ("gpt" in model_lower or "openai" in model_lower):
            actual_model = "gpt-4o" if "gpt" in model_lower else model
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
                    async with session.post(url, headers=headers, json=payload, timeout=timeout or 30.0) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            res_str = data["choices"][0]["message"]["content"]
                            return RouteOutput(response=res_str, latency=time.time() - start_time)
                        else:
                            err_msg = f"OpenAI API returned status {resp.status}"
                            print(f"[ROUTE: byok] {err_msg}")
                            return RouteOutput(latency=time.time() - start_time, error=err_msg)
            except Exception as e:
                err_msg = str(e)
                print(f"[ROUTE: byok] OpenAI API call failed: {e}")
                return RouteOutput(latency=time.time() - start_time, error=err_msg)

        return RouteOutput(latency=time.time() - start_time, error="No keys or model unsupported")
