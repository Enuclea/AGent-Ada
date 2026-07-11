import os
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from fastapi import HTTPException
from pydantic import BaseModel

from agent.api.router import app
from agent import memory

class WorkerRegistrationRequest(BaseModel):
    worker_id: str
    host: str
    capabilities: List[str] = []
    platform: Optional[str] = None
    max_concurrent: int = 3
    has_agy: bool = False
    has_grok: bool = False
    python_version: Optional[str] = None
    ollama_models: List[str] = []

@app.post("/api/workers/register")
async def register_worker_endpoint(req: WorkerRegistrationRequest):
    # Validate worker registration host to prevent SSRF or metadata traversal
    import urllib.parse
    raw_host = req.host
    if not raw_host.startswith(("http://", "https://")):
        raw_host = f"http://{raw_host}"
    try:
        parsed = urllib.parse.urlparse(raw_host)
        hostname = parsed.hostname
        port = parsed.port
        if not hostname:
            raise HTTPException(status_code=400, detail="Invalid host format")
        if hostname == "169.254.169.254":
            raise HTTPException(status_code=400, detail="Metadata IP registration is forbidden")
        if port is not None and not (1 <= port <= 65535):
            raise HTTPException(status_code=400, detail="Invalid port number")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid worker host URL: {e}")

    metadata = {}
    if req.python_version:
        metadata["python_version"] = req.python_version
    if req.ollama_models:
        metadata["ollama_models"] = req.ollama_models

    memory.register_worker(
        worker_id=req.worker_id,
        host=req.host,
        capabilities=req.capabilities,
        platform_name=req.platform or "",
        max_concurrent=req.max_concurrent,
        has_agy=req.has_agy,
        has_grok=req.has_grok,
        metadata=metadata,
    )
    print(f"[WORKERS] Registered worker '{req.worker_id}' at {req.host} with capabilities: {req.capabilities}")
    if req.ollama_models:
        print(f"[WORKERS]   Ollama models: {', '.join(req.ollama_models)}")
    return {"status": "success", "worker_id": req.worker_id}

@app.get("/api/workers")
async def list_workers_endpoint():
    workers = memory.get_registered_workers()
    return {"workers": workers}

@app.get("/api/workers/{worker_id}/health")
async def check_worker_health_endpoint(worker_id: str):
    workers = memory.get_registered_workers()
    target = next((w for w in workers if w["worker_id"] == worker_id), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"Worker '{worker_id}' not found")

    from agent.remote_worker import check_worker_health
    is_healthy = await check_worker_health(target)
    return {
        "worker_id": worker_id,
        "healthy": is_healthy,
        "status": "online" if is_healthy else "offline",
    }

@app.delete("/api/workers/{worker_id}")
async def remove_worker_endpoint(worker_id: str):
    memory.remove_worker(worker_id)
    return {"status": "success", "detail": f"Worker '{worker_id}' removed"}

@app.get("/api/config/tenants")
async def get_tenant_instances():
    ports_path = Path("/home/ada/public_ada_bot/ports.json")
    if not ports_path.exists():
        ports_path = Path.home() / "AGent" / "scratch" / "public_ada_bot" / "ports.json"
    if not ports_path.exists():
        ports_path = Path("/home/dan/AGent/scratch/public_ada_bot/ports.json")
        
    tenants = []
    if ports_path.exists():
        try:
            with open(ports_path, "r") as f:
                ports_data = json.load(f)
            for owner_id, info in ports_data.items():
                port = info.get("port") if isinstance(info, dict) else info
                tenants.append({
                    "owner_id": owner_id,
                    "port": port,
                    "status": "RUNNING"
                })
        except Exception:
            pass
    return {"status": "success", "tenants": tenants}

@app.post("/api/config/tenants/{owner_id}/{action}")
async def control_tenant_instance(owner_id: str, action: str):
    import asyncio
    import re
    if action not in ("up", "down"):
        raise HTTPException(status_code=400, detail="Invalid action")
    if not re.match(r"^[a-zA-Z0-9_-]+$", owner_id):
        raise HTTPException(status_code=400, detail="Invalid owner_id format")
        
    try:
        host_ip = os.environ.get("HOST_COMMAND_CHANNEL_IP", "127.0.0.1")
        reader, writer = await asyncio.open_connection(host_ip, 8002)
        
        payload = {"action": action, "owner_id": owner_id}
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        
        data = await reader.read(4096)
        response = json.loads(data.decode().strip())
        writer.close()
        await writer.wait_closed()
        
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to communicate with host command channel: {e}")

class DiscordMembersRequest(BaseModel):
    members_data: dict

class DiscordConfigRequest(BaseModel):
    config_data: dict

class TaskLogRequest(BaseModel):
    message: str

class TaskStatusRequest(BaseModel):
    status: str

class ScheduleRequest(BaseModel):
    name: str
    prompt: str
    cron_expr: str

@app.post("/api/discord/members")
async def post_discord_members(req: DiscordMembersRequest):
    try:
        members_file = Path("/data/members.json")
        members_file.parent.mkdir(parents=True, exist_ok=True)
        with open(members_file, "w", encoding="utf-8") as f:
            json.dump(req.members_data, f, indent=2, ensure_ascii=False)
            
        mem = memory.load_memory()
        mem.setdefault("key_value", {})["discord_members"] = req.members_data
        memory.save_memory(mem)
        
        return {"status": "success", "message": "Discord members synchronized successfully"}
    except Exception as e:
        import traceback
        print("[ERROR] Exception in post_discord_members:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to synchronize Discord members: {e}")

@app.get("/api/discord/members")
async def get_discord_members():
    try:
        members_file = Path("/data/members.json")
        def _read():
            if members_file.exists():
                with open(members_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            return None

        data = await asyncio.to_thread(_read)
        if data is not None:
            return data
        
        mem = memory.load_memory()
        return mem.get("key_value", {}).get("discord_members", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve Discord members: {e}")

def load_modules():
    modules = []
    
    # 1. Legacy: scan static/modules/ for module descriptors
    modules_dir = Path(__file__).parent.parent / "static" / "modules"
    if modules_dir.exists():
        for path in modules_dir.iterdir():
            if path.is_dir():
                config_file = path / "module.json"
                if config_file.exists():
                    try:
                        with open(config_file, "r") as f:
                            data = json.load(f)
                            data["id"] = path.name
                            data.setdefault("enabled", True)
                            if data.get("enabled"):
                                modules.append(data)
                    except Exception as e:
                        print(f"Error loading module config from {path}: {e}")
    
    # 2. Plugins: scan active plugin directories for static/module.json
    try:
        from agent.core.plugins import plugin_manager, PluginState
        for name, plugin in plugin_manager.plugins.items():
            if plugin.state == PluginState.ACTIVE:
                config_file = plugin.path / "static" / "module.json"
                if config_file.exists():
                    try:
                        with open(config_file, "r") as f:
                            data = json.load(f)
                            data["id"] = name
                            data.setdefault("enabled", True)
                            if data.get("enabled"):
                                modules.append(data)
                    except Exception as e:
                        print(f"Error loading plugin module config from {name}: {e}")
    except Exception:
        pass  # Plugin system not available
    
    return modules

@app.get("/api/modules")
async def list_modules_endpoint():
    import asyncio
    modules = await asyncio.to_thread(load_modules)
    return {"modules": modules}

@app.post("/api/discord/config")
async def post_discord_config(req: DiscordConfigRequest):
    try:
        config_file = Path(__file__).resolve().parents[3] / "discord" / "config.json"
        def _write():
            config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(req.config_data, f, indent=2, ensure_ascii=False)

        import asyncio
        await asyncio.to_thread(_write)
            
        mem = memory.load_memory()
        mem.setdefault("key_value", {})["discord_config"] = req.config_data
        memory.save_memory(mem)
        
        return {"status": "success", "message": "Discord config synchronized successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to synchronize Discord config: {e}")

@app.get("/api/discord/config")
async def get_discord_config():
    try:
        config_file = Path(__file__).resolve().parents[3] / "discord" / "config.json"
        def _read():
            if config_file.exists():
                with open(config_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            return None

        import asyncio
        data = await asyncio.to_thread(_read)
        if data is not None:
            return data
        
        mem = memory.load_memory()
        return mem.get("key_value", {}).get("discord_config", {
            "default_model": "gemini-3.5-flash",
            "channels": {}
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve Discord config: {e}")

@app.post("/api/tasks/{task_id}/log")
async def log_task_endpoint(task_id: str, req: TaskLogRequest):
    memory.add_task_log(task_id, req.message)
    return {"status": "success"}

@app.post("/api/tasks/{task_id}/status")
async def status_task_endpoint(task_id: str, req: TaskStatusRequest):
    memory.update_active_task_status(task_id, req.status)
    return {"status": "success"}

@app.get("/api/tasks/{task_id}/logs")
async def get_task_logs_endpoint(task_id: str):
    clean_id = task_id.replace("task-agent-", "")
    if clean_id.startswith("subagent-") or clean_id.startswith("boardroom-") or task_id.startswith("subagent-"):
        try:
            msgs = memory.get_subagent_messages(clean_id)
            logs = []
            for m in msgs:
                if m["role"] == "parent":
                    continue
                logs.append({
                    "timestamp": m["timestamp"],
                    "message": m["message"]
                })
            return {"logs": logs}
        except Exception as e:
            print(f"Error fetching subagent logs for task feed: {e}")
            return {"logs": []}
    logs = memory.get_task_logs(task_id)
    return {"logs": logs}

@app.get("/api/schedule")
async def list_schedule_endpoint():
    from agent.core.scheduler import ensure_default_scheduled_tasks
    ensure_default_scheduled_tasks()
    schedules = memory.get_scheduled_tasks()
    return {"schedules": schedules}

@app.post("/api/schedule")
async def create_schedule_endpoint(req: ScheduleRequest):
    import uuid
    from datetime import datetime, timezone
    from agent.core.scheduler import get_next_cron_run
    schedule_id = str(uuid.uuid4())
    try:
        next_run_dt = get_next_cron_run(req.cron_expr, datetime.now(timezone.utc))
        next_run = next_run_dt.isoformat()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression or interval: {e}")
    memory.add_scheduled_task(schedule_id, req.name, req.prompt, req.cron_expr, next_run)
    return {"status": "success", "id": schedule_id, "next_run": next_run}

@app.delete("/api/schedule/{schedule_id}")
async def delete_schedule_endpoint(schedule_id: str):
    memory.delete_scheduled_task(schedule_id)
    return {"status": "success"}

