import os
import json
import asyncio
import aiohttp
from pathlib import Path
from typing import List, Optional, Union
from agent.routes.base import BaseRoute, RouteStatus, RouteInput, RouteOutput

def load_ollama_hosts() -> List[str]:
    """Resolves and loads Ollama server hosts from configuration or defaults.
    
    If no config file exists, it auto-generates a default config at config/ollama_hosts.json
    so the user can edit it.
    """
    env_config = os.environ.get("OLLAMA_HOSTS_CONFIG")
    if env_config:
        config_path = Path(env_config)
    else:
        config_path = Path("config/ollama_hosts.json")

    # If local path doesn't exist, try standard home fallback
    if not config_path.exists():
        home_config = Path.home() / ".agent" / "ollama_hosts.json"
        if home_config.exists():
            config_path = home_config
        else:
            # Auto-generate a default JSON file in config/ollama_hosts.json
            try:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                default_data = {
                    "hosts": [
                        "http://10.200.0.4:11434",
                        "http://10.200.0.3:11434",
                        "http://localhost:11434"
                    ]
                }
                with open(config_path, "w") as f:
                    json.dump(default_data, f, indent=2)
                print(f"[OLLAMA] Auto-generated default config file at: {config_path}")
            except Exception as e:
                print(f"[OLLAMA] Failed to auto-generate config: {e}")
                return ["http://10.200.0.4:11434", "http://10.200.0.3:11434", "http://localhost:11434"]

    try:
        with open(config_path, "r") as f:
            data = json.load(f)
            if "hosts" in data and isinstance(data["hosts"], list):
                return [str(h) for h in data["hosts"] if h]
    except Exception as e:
        print(f"[OLLAMA] Failed to parse config file {config_path}: {e}")

    return ["http://10.200.0.4:11434", "http://10.200.0.3:11434", "http://localhost:11434"]

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

        actual_model = model.replace("ollama/", "")
        
        # Build prompt
        full_prompt = prompt
        if system_instructions:
            full_prompt = f"[System Instructions]\n{system_instructions}\n\n[User Prompt]\n{prompt}"

        # Resolve target URLs dynamically from config / env
        hosts_env = os.environ.get("OLLAMA_SERVERS") or os.environ.get("OLLAMA_HOSTS")
        if hosts_env:
            raw_hosts = [h.strip() for h in hosts_env.split(",") if h.strip()]
        else:
            single_host = os.environ.get("OLLAMA_HOST")
            if single_host:
                raw_hosts = [single_host]
            else:
                # Load from the formal config file
                raw_hosts = load_ollama_hosts()

        urls = []
        for host in raw_hosts:
            if not host.startswith("http"):
                host = f"http://{host}"
            if not host.endswith("/api/generate") and not host.endswith("/api/chat"):
                host = host.rstrip("/") + "/api/generate"
            urls.append(host)

        payload = {
            "model": actual_model,
            "prompt": full_prompt,
            "stream": False
        }

        last_err = "No workers configured"
        for url in urls:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=timeout or 60.0) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            res_str = data["response"]
                            return RouteOutput(response=res_str, latency=time.time() - start_time)
                        else:
                            last_err = f"Worker {url} returned status {resp.status}"
                            print(f"[ROUTE: ollama] {last_err}")
            except Exception as e:
                last_err = str(e)
                print(f"[ROUTE: ollama] Worker {url} failed: {e}")
        
        return RouteOutput(latency=time.time() - start_time, error=last_err)
