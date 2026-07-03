import os
import asyncio
import aiohttp
from typing import List, Optional
from agent.routes.base import BaseRoute, RouteStatus

class OllamaRoute(BaseRoute):
    @property
    def name(self) -> str:
        return "ollama"

    @property
    def default_status(self) -> RouteStatus:
        return RouteStatus.SECONDARY

    @property
    def default_priority(self) -> int:
        return 30

    @property
    def supported_models(self) -> List[str]:
        # Matches models prefix matching "ollama/"
        return ["ollama/"]

    async def execute(
        self,
        prompt: str,
        model: str,
        system_instructions: Optional[str] = None,
        timeout: Optional[float] = None,
        conversation_id: Optional[str] = None,
    ) -> Optional[str]:
        actual_model = model.replace("ollama/", "")
        
        # Build prompt
        full_prompt = prompt
        if system_instructions:
            full_prompt = f"[System Instructions]\n{system_instructions}\n\n[User Prompt]\n{prompt}"

        # Try both the primary and fallback worker interfaces
        urls = ["http://10.200.0.4:11434/api/generate", "http://10.200.0.3:11434/api/generate"]
        payload = {
            "model": actual_model,
            "prompt": full_prompt,
            "stream": False
        }

        for url in urls:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=timeout or 60.0) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data["response"]
                        else:
                            print(f"[ROUTE: ollama] Worker {url} returned status {resp.status}")
            except Exception as e:
                print(f"[ROUTE: ollama] Worker {url} failed: {e}")
        
        return None
