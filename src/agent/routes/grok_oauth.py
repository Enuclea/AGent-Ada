import os
import json
import time
import aiohttp
from pathlib import Path
from typing import List, Optional, Union
from agent.routes.base import BaseRoute, RouteStatus, RouteInput, RouteOutput

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
API_URL = "https://api.x.ai/v1/chat/completions"

class GrokOAuthRoute(BaseRoute):
    @property
    def name(self) -> str:
        return "grok-oauth"

    @property
    def default_status(self) -> RouteStatus:
        return RouteStatus.SECONDARY

    @property
    def default_priority(self) -> int:
        return 15

    @property
    def supported_models(self) -> List[str]:
        return ["grok"]

    def _get_oauth_path(self) -> Path:
        return Path.home() / ".agent" / "xai_oauth.json"

    def supports_model(self, model: str) -> bool:
        model_lower = model.lower()
        if "grok" not in model_lower:
            return False
        return self._get_oauth_path().exists()

    async def _get_valid_token(self) -> Optional[str]:
        path = self._get_oauth_path()
        if not path.exists():
            return None

        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[ROUTE: grok-oauth] Error reading token file: {e}")
            return None

        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_at = data.get("expires_at", 0.0)

        # Refresh token if it expires in less than 5 minutes
        if time.time() + 300 >= expires_at:
            if not refresh_token:
                print("[ROUTE: grok-oauth] Access token expired and no refresh token available.")
                return None

            print("[ROUTE: grok-oauth] Access token expired or expiring soon. Refreshing...")
            payload = {
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(TOKEN_URL, data=payload, timeout=15.0) as resp:
                        if resp.status == 200:
                            res_data = await resp.json()
                            access_token = res_data.get("access_token")
                            refresh_token = res_data.get("refresh_token") or refresh_token
                            expires_in = res_data.get("expires_in", 3600)
                            
                            # Save refreshed tokens
                            updated_data = {
                                "access_token": access_token,
                                "refresh_token": refresh_token,
                                "expires_at": time.time() + expires_in
                            }
                            path.parent.mkdir(parents=True, exist_ok=True)
                            with open(path, "w") as f_out:
                                json.dump(updated_data, f_out, indent=2)
                            print("[ROUTE: grok-oauth] Token refreshed successfully.")
                        else:
                            err_txt = await resp.text()
                            print(f"[ROUTE: grok-oauth] Token refresh failed: status={resp.status}, body={err_txt}")
                            return None
            except Exception as e:
                print(f"[ROUTE: grok-oauth] Exception during token refresh: {e}")
                return None

        return access_token

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
            model = model or "grok-build-0.1"

        # Map generic 'grok' or fallback models to default active Grok model
        if model == "grok" or not model or model == "*":
            model = "grok-build-0.1"

        access_token = await self._get_valid_token()
        if not access_token:
            err_msg = "OAuth authorization token not found or expired."
            print(f"[ROUTE: grok-oauth] {err_msg}")
            return RouteOutput(latency=time.time() - start_time, error=err_msg)

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        messages = []
        if system_instructions:
            messages.append({"role": "system", "content": system_instructions})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(API_URL, headers=headers, json=payload, timeout=timeout or 60.0) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        res_str = data["choices"][0]["message"]["content"]
                        return RouteOutput(response=res_str, latency=time.time() - start_time)
                    else:
                        err_txt = await resp.text()
                        err_msg = f"xAI API returned status {resp.status}: {err_txt}"
                        print(f"[ROUTE: grok-oauth] {err_msg}")
                        return RouteOutput(latency=time.time() - start_time, error=err_msg)
        except Exception as e:
            err_msg = str(e)
            print(f"[ROUTE: grok-oauth] API call failed: {e}")
            return RouteOutput(latency=time.time() - start_time, error=err_msg)
