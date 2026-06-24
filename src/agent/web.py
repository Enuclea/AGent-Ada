import asyncio
import os
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import memory, tools, __version__
from google.antigravity import Agent, LocalAgentConfig
from google.antigravity.hooks import policy, hooks
from google.antigravity.types import CapabilitiesConfig, ToolCall, ModelTarget, ModelType
from agent.keyless import KeylessGeminiAPIEndpoint, setup_keyless_environment, KeylessAgyAgent

class PriorityLock:
    """Acquires a lock sequentially based on request priority (lowest integer value = highest priority)."""
    def __init__(self) -> None:
        self._waiters = []  # list of (priority, asyncio.Future)
        self._locked = False

    async def acquire(self, priority: int) -> None:
        if not self._locked:
            self._locked = True
            return

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._waiters.append((priority, fut))
        self._waiters.sort(key=lambda x: x[0])

        try:
            await fut
        except asyncio.CancelledError:
            self._waiters = [w for w in self._waiters if w[1] != fut]
            if not self._locked:
                self._release_next()
            raise

    def release(self) -> None:
        self._locked = False
        self._release_next()

    def _release_next(self) -> None:
        if self._waiters:
            self._locked = True
            priority, fut = self._waiters.pop(0)
            if not fut.done():
                fut.set_result(None)

backend_lock = PriorityLock()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    scheduler_task = asyncio.create_task(run_scheduler())
    yield
    # Shutdown
    scheduler_task.cancel()

app = FastAPI(title="Ada Task Engine Dashboard", lifespan=lifespan)

# Global state to maintain active session
active_agents = {}  # session_id -> dict

class ChatRequest(BaseModel):
    prompt: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    system_instructions: Optional[str] = None
    disable_tools: Optional[bool] = False

class DiscordMembersRequest(BaseModel):
    members_data: dict

class DiscordConfigRequest(BaseModel):
    config_data: dict

async def get_or_create_agent(
    model_name: Optional[str] = None,
    session_id: Optional[str] = None,
    system_instructions: Optional[str] = None,
    disable_tools: bool = False
):
    """Retrieves the active Agent connection or builds a new one."""
    global active_agents
    
    model_name = model_name or "gemini-3.5-flash"
    save_dir = Path.home() / ".agent" / "sessions"
    save_dir.mkdir(parents=True, exist_ok=True)
    
    resolved_id = None
    is_discord = False
    
    if session_id:
        if session_id.startswith("discord-session-"):
            is_discord = True
            mem = memory.load_memory()
            session_mappings = mem.setdefault("key_value", {}).get("session_mappings", {})
            if not isinstance(session_mappings, dict):
                session_mappings = {}
                mem["key_value"]["session_mappings"] = session_mappings
            
            mapped_id = session_mappings.get(session_id)
            keyless_dir = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
            if mapped_id and ((save_dir / f"{mapped_id}.db").exists() or (keyless_dir / f"{mapped_id}.db").exists()):
                resolved_id = mapped_id
        else:
            keyless_dir = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
            if (save_dir / f"{session_id}.db").exists() or (keyless_dir / f"{session_id}.db").exists():
                resolved_id = session_id

    # We map by the passed session_id or a default key
    lookup_id = session_id or "default"
    
    # Check if we need to reconstruct due to model, session, instructions, or tool status change
    session_data = active_agents.get(lookup_id)
    if session_data is not None:
        agent = session_data["agent"]
        needs_reconstruct = False
        if model_name and session_data["model"] != model_name:
            needs_reconstruct = True
        elif session_id:
            if resolved_id is None or agent.conversation_id != resolved_id:
                needs_reconstruct = True
        if system_instructions and session_data["instructions"] != system_instructions:
            needs_reconstruct = True
        if disable_tools != session_data["disable_tools"]:
            needs_reconstruct = True
                
        if needs_reconstruct:
            try:
                await agent.__aexit__(None, None, None)
            except Exception:
                pass
            active_agents.pop(lookup_id, None)

    if lookup_id not in active_agents:
        resolved_id_to_pass = resolved_id
        
        # Load API Key if available (ignored by default to enforce keyless agy CLI execution)
        api_key = None

        # Construct instructions and configure capabilities/tools/policies
        memory.active_session_id = session_id
        if system_instructions:
            full_instructions = system_instructions
        else:
            memory_summary = memory.get_fact_summary()
            installed_skills = tools.list_installed_skills()
            base_instructions = (
                "You are Ada, the autonomous AI developer assistant behind the Ada Task Engine, powered by AntiGravity.\n"
                "You help the user write, test, debug, and manage code in their workspace.\n"
                "Always be concise, professional, and helpful.\n\n"
                "SELF-IMPROVEMENT & TOOL BUILDING:\n"
                "- You have the ability to record facts about the user/project using `record_memory_fact`.\n"
                "- You can record key-value pairs using `record_memory_key_value`.\n"
                "- You can autonomously write new custom tools and skills using `create_agent_skill`, or modify/expand "
                "existing custom skills (such as fixing bugs or adding scripts) using `improve_agent_skill`.\n"
                "- When you successfully solve a non-trivial problem, figure out a complex workflow, or build a helper script, "
                "you should save it as a reusable skill using `create_agent_skill` (or refine it using `improve_agent_skill`) "
                "so you (or other agents) can reload it in future runs.\n"
                "- You can search past conversations and sessions using `search_past_conversations`. Whenever the user "
                "asks about previous tasks, context, or decisions, use `search_past_conversations` to recall what you did.\n"
                "- Before starting any complex task, check the list of installed skills to see if you have relevant custom tools.\n\n"
                "RUNNING LONG-RUNNING COMMANDS & PROCESSES:\n"
                "- Any long-running command, service, server, background daemon, or Discord bot (like `discord/bot.py`) MUST be executed in the background using `nohup` and backgrounded, for example: `PYTHONPATH=src nohup .venv/bin/python3 discord/bot.py > discord/bot.log 2>&1 &`.\n"
                "- You must NEVER run on a persistent process or bot in the foreground, as it blocks the tool execution and hangs the agent connection.\n"
                "- Never use interactive prompts or tail commands that block indefinitely (e.g. `tail -f`). Always ensure your commands exit immediately.\n\n"
                "WORKSPACE SAFETY & DIRECTORY STRUCTURE:\n"
                "- When writing, testing, or editing code to complete user requests, you must write files to the appropriate project directories (e.g. `src/` or `scratch/`).\n"
                "- You must NEVER write, modify, or create project code files inside the `discord/` directory unless you are specifically asked to edit the Discord bot code itself. Keep the `discord/` folder isolated strictly for the bot's system files."
            )
            full_instructions = base_instructions
            if memory_summary:
                full_instructions += f"\n\n{memory_summary}"
            if "No custom skills installed" not in installed_skills:
                full_instructions += f"\n\n[INSTALLED CUSTOM SKILLS/TOOLS]\n{installed_skills}\n[END OF INSTALLED CUSTOM SKILLS/TOOLS]"

            if disable_tools:
                capabilities = CapabilitiesConfig(enable_subagents=False)
                custom_tools = []
                policies = [policy.allow_all()]
            else:
                capabilities = CapabilitiesConfig(enable_subagents=True)
                custom_tools = [
                    tools.record_memory_fact,
                    tools.record_memory_key_value,
                    tools.create_agent_skill,
                    tools.improve_agent_skill,
                    tools.list_installed_skills,
                    tools.search_past_conversations,
                    tools.list_repository_skills,
                    tools.view_repository_skill_code,
                    tools.install_repository_skill,
                ]

        current_active_task_id = None

        @hooks.post_tool_call
        async def on_post_tool(data):
            nonlocal current_active_task_id
            if current_active_task_id:
                memory.update_active_task_status(current_active_task_id, "completed")
                current_active_task_id = None

        @hooks.on_tool_error
        async def on_tool_err(err):
            nonlocal current_active_task_id
            if current_active_task_id:
                memory.update_active_task_status(current_active_task_id, "failed")
                current_active_task_id = None

        async def my_approval_handler(tool_call: ToolCall) -> bool:
            nonlocal current_active_task_id
            import uuid
            task_id = str(uuid.uuid4())
            current_active_task_id = task_id
            
            # Record in active tasks tracker
            memory.add_active_task(task_id, tool_call.name, str(tool_call.args))
            
            # Log step to database history
            memory.log_conversation_step(
                agent.conversation_id,
                "tool_call",
                str(tool_call.args),
                tool_name=tool_call.name
            )
            return True

        if not disable_tools:
            policies = [
                policy.ask_user("run_command", handler=my_approval_handler),
                policy.ask_user("create_file", handler=my_approval_handler),
                policy.ask_user("edit_file", handler=my_approval_handler),
                policy.ask_user("start_subagent", handler=my_approval_handler),
                policy.allow_all(),
            ]

        if api_key:
            config_args = {
                "system_instructions": full_instructions,
                "capabilities": capabilities,
                "tools": custom_tools,
                "policies": policies,
                "workspaces": [os.getcwd()],
                "save_dir": str(save_dir),
                "skills_paths": [str(tools.SKILLS_DIR)],
                "hooks": [on_post_tool, on_tool_err],
                "api_key": api_key,
            }
            if model_name:
                config_args["model"] = model_name
            if resolved_id_to_pass:
                config_args["conversation_id"] = resolved_id_to_pass

            config = LocalAgentConfig(**config_args)
            agent_conn = Agent(config)
            agent = await agent_conn.__aenter__()
        else:
            # Keyless setup using KeylessAgyAgent
            agent = KeylessAgyAgent(
                model=model_name or "gemini-3.5-flash",
                system_instructions=full_instructions,
                conversation_id=resolved_id_to_pass,
            )
            agent = await agent.__aenter__()
        
        # Save mapping back if this was a new discord-session
        if is_discord and not resolved_id:
            mem = memory.load_memory()
            session_mappings = mem.setdefault("key_value", {}).setdefault("session_mappings", {})
            session_mappings[session_id] = agent.conversation_id
            memory.save_memory(mem)

        active_agents[lookup_id] = {
            "agent": agent,
            "model": model_name or "gemini-3.5-flash",
            "instructions": system_instructions,
            "disable_tools": disable_tools
        }

    return active_agents[lookup_id]["agent"]

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    global active_agents
    try:
        agent = await get_or_create_agent(req.model, req.session_id, req.system_instructions, req.disable_tools)
    except Exception as e:
        import traceback
        traceback.print_exc()
        lookup_id = req.session_id or "default"
        active_agents.pop(lookup_id, None)
        raise HTTPException(status_code=500, detail=f"Failed to get/create agent: {e}")

    session_id = req.session_id or ""
    if not session_id or not session_id.startswith("discord-"):
        priority = 0  # API / local admin (highest priority)
    elif session_id.startswith("discord-session-"):
        priority = 1  # Discord admin
    else:
        priority = 2  # Discord general / moderator

    async def event_generator():
        global active_agents
        lookup_id = req.session_id or "default"
        await backend_lock.acquire(priority)
        try:
            # Log user prompt
            memory.log_conversation_step(agent.conversation_id, "user", req.prompt)
            
            # Run the agent execution
            response = await agent.chat(req.prompt)
            
            # Yield session ID first
            yield f"data: {json.dumps({'type': 'session_id', 'content': agent.conversation_id})}\n\n"
            
            # Stream thoughts
            thoughts_str = ""
            async for thought in response.thoughts:
                thoughts_str += thought
                yield f"data: {json.dumps({'type': 'thought', 'content': thought})}\n\n"
                
            if thoughts_str:
                memory.log_conversation_step(agent.conversation_id, "thought", thoughts_str)
                
            # Stream response chunks
            output_content = ""
            async for chunk in response:
                output_content += chunk
                yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"
                
            if output_content:
                memory.log_conversation_step(agent.conversation_id, "assistant", output_content)
                
            # Complete
            yield "data: [DONE]\n\n"
        except Exception as e:
            import traceback
            traceback.print_exc()
            active_agents.pop(lookup_id, None)
            yield f"data: {json.dumps({'type': 'error', 'content': f'Agent connection error: {e}'})}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            backend_lock.release()

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/api/status")
async def status_endpoint():
    global active_agents
    try:
        agent = await get_or_create_agent()
    except Exception as e:
        active_agents.pop("default", None)
        raise HTTPException(status_code=500, detail=f"Agent connection error: {e}")
    
    skills = []
    if tools.SKILLS_DIR.exists() and tools.SKILLS_DIR.is_dir():
        for folder in tools.SKILLS_DIR.iterdir():
            if folder.is_dir():
                skill_md = folder / "SKILL.md"
                if skill_md.exists() and skill_md.is_file():
                    try:
                        with open(skill_md, "r", encoding="utf-8") as f:
                            content = f.read()
                        fm = tools._parse_frontmatter(content)
                        name = fm.get("name", folder.name)
                        desc = fm.get("description", "No description.")
                        skills.append({"name": name, "description": desc})
                    except Exception:
                        continue
                        
    session_data = active_agents.get("default", {})
    return {
        "status": "busy" if backend_lock._locked else "ready",
        "version": __version__,
        "model": session_data.get("model", "gemini-3.5-flash"),
        "workspace": os.getcwd(),
        "session_id": agent.conversation_id,
        "skills": skills
    }

@app.get("/api/tasks")
async def tasks_endpoint():
    tasks = memory.get_active_tasks()
    return {"tasks": tasks}

@app.get("/api/history")
async def history_endpoint(session_id: Optional[str] = None):
    global active_agents
    resolved_id = None
    if session_id:
        # resolve session ID mapping if it's a discord-session-
        save_dir = Path.home() / ".agent" / "sessions"
        if session_id.startswith("discord-session-"):
            mem = memory.load_memory()
            session_mappings = mem.get("key_value", {}).get("session_mappings", {})
            if isinstance(session_mappings, dict):
                resolved_id = session_mappings.get(session_id)
        else:
            resolved_id = session_id
    else:
        # fallback to default session's conversation ID
        default_session = active_agents.get("default")
        if default_session:
            resolved_id = default_session["agent"].conversation_id

    if not resolved_id:
        return {"history": []}

    conn = sqlite3.connect(memory.DB_FILE_PATH)
    history = []
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, content, tool_name, timestamp FROM conversation_steps WHERE session_id = ? ORDER BY id ASC",
            (resolved_id,)
        )
        rows = cursor.fetchall()
        for row in rows:
            history.append({
                "role": row[0],
                "content": row[1],
                "tool_name": row[2],
                "timestamp": row[3]
            })
    except Exception:
        pass
    finally:
        conn.close()
    return {"history": history}

@app.get("/api/sessions")
async def sessions_endpoint():
    save_dir = Path.home() / ".agent" / "sessions"
    keyless_dir = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
    entries = []
    seen = set()
    
    for folder in [save_dir, keyless_dir]:
        if folder.exists() and folder.is_dir():
            for entry in folder.iterdir():
                if entry.name.startswith(".") or not entry.name.endswith(".db"):
                    continue
                stat = entry.stat()
                name = entry.name[:-3]
                if name in seen:
                    continue
                seen.add(name)
                entries.append({
                    "session_id": name,
                    "last_active": datetime.fromtimestamp(stat.st_mtime).isoformat()
                })
    entries.sort(key=lambda x: x["last_active"], reverse=True)
    return {"sessions": entries}

@app.post("/api/sessions/resume")
async def resume_session_endpoint(data: dict):
    session_id = data.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    await get_or_create_agent(session_id=session_id)
    return {"status": "success", "session_id": session_id}

# --- Discord Brokered Hook Endpoints ---

@app.post("/api/discord/members")
async def post_discord_members(req: DiscordMembersRequest):
    """Refreshes the centralized cache of connected Discord members/guilds."""
    try:
        # Save to discord/members.json (the local fallback)
        members_file = Path(__file__).parent.parent.parent / "discord" / "members.json"
        members_file.parent.mkdir(parents=True, exist_ok=True)
        with open(members_file, "w", encoding="utf-8") as f:
            json.dump(req.members_data, f, indent=2, ensure_ascii=False)
            
        # Also store in persistent memory so the agent loop always has immediate access
        mem = memory.load_memory()
        mem.setdefault("key_value", {})["discord_members"] = req.members_data
        memory.save_memory(mem)
        
        return {"status": "success", "message": "Discord members synchronized successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to synchronize Discord members: {e}")

@app.get("/api/discord/members")
async def get_discord_members():
    """Retrieves the centrally cached list of Discord accounts/members."""
    try:
        members_file = Path(__file__).parent.parent.parent / "discord" / "members.json"
        if members_file.exists():
            with open(members_file, "r", encoding="utf-8") as f:
                return json.load(f)
        
        # Fallback to persistent memory cache
        mem = memory.load_memory()
        return mem.get("key_value", {}).get("discord_members", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve Discord members: {e}")

@app.post("/api/discord/config")
async def post_discord_config(req: DiscordConfigRequest):
    """Sets/updates the centralized channel/bot configuration."""
    try:
        config_file = Path(__file__).parent.parent.parent / "discord" / "config.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(req.config_data, f, indent=2, ensure_ascii=False)
            
        # Also cache in memory settings
        mem = memory.load_memory()
        mem.setdefault("key_value", {})["discord_config"] = req.config_data
        memory.save_memory(mem)
        
        return {"status": "success", "message": "Discord config synchronized successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to synchronize Discord config: {e}")

@app.get("/api/discord/config")
async def get_discord_config():
    """Retrieves the centrally brokered channel configuration."""
    try:
        config_file = Path(__file__).parent.parent.parent / "discord" / "config.json"
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        
        # Fallback to persistent memory cache
        mem = memory.load_memory()
        return mem.get("key_value", {}).get("discord_config", {
            "default_model": "gemini-3.5-flash",
            "channels": {}
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve Discord config: {e}")

# Models for Logging and Schedules
class TaskLogRequest(BaseModel):
    message: str

class TaskStatusRequest(BaseModel):
    status: str

class ScheduleRequest(BaseModel):
    name: str
    prompt: str
    cron_expr: str

# Endpoints for task progress updates and subagent hooks
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
    logs = memory.get_task_logs(task_id)
    return {"logs": logs}

# Endpoints for schedules
@app.get("/api/schedule")
async def list_schedule_endpoint():
    schedules = memory.get_scheduled_tasks()
    return {"schedules": schedules}

@app.post("/api/schedule")
async def create_schedule_endpoint(req: ScheduleRequest):
    import uuid
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

# Cron validation and parser functions
def match_cron_field(field_val: int, pattern: str, range_min: int, range_max: int) -> bool:
    if pattern == "*":
        return True
    if pattern.startswith("*/"):
        try:
            step = int(pattern[2:])
            return (field_val % step) == 0
        except ValueError:
            return False
    if "," in pattern:
        return any(match_cron_field(field_val, p, range_min, range_max) for p in pattern.split(","))
    if "-" in pattern:
        try:
            start, end = map(int, pattern.split("-"))
            return start <= field_val <= end
        except ValueError:
            return False
    try:
        return int(pattern) == field_val
    except ValueError:
        return False

def get_next_cron_run(cron_expr: str, from_dt: datetime) -> datetime:
    from datetime import timedelta
    if cron_expr.isdigit():
        return from_dt + timedelta(seconds=int(cron_expr))
    
    parts = cron_expr.split()
    if len(parts) != 5:
        return from_dt + timedelta(seconds=60)
        
    min_pat, hour_pat, dom_pat, month_pat, dow_pat = parts
    check_dt = from_dt + timedelta(minutes=1)
    check_dt = check_dt.replace(second=0, microsecond=0)
    
    for _ in range(525600):
        cron_dow = check_dt.weekday() + 1
        if cron_dow == 7:
            cron_dow = 0
        
        if (match_cron_field(check_dt.minute, min_pat, 0, 59) and
            match_cron_field(check_dt.hour, hour_pat, 0, 23) and
            match_cron_field(check_dt.day, dom_pat, 1, 31) and
            match_cron_field(check_dt.month, month_pat, 1, 12) and
            (match_cron_field(cron_dow, dow_pat, 0, 6) or match_cron_field(check_dt.weekday() + 1, dow_pat, 1, 7))):
            return check_dt
        
        check_dt += timedelta(minutes=1)
    
    return from_dt + timedelta(hours=1)

# Background scheduler execution
async def execute_scheduled_task(name: str, prompt: str):
    global active_agents
    try:
        agent = await get_or_create_agent()
        memory.log_conversation_step(agent.conversation_id, "user", f"[Scheduled Task: {name}] {prompt}")
        response = await agent.chat(prompt)
        
        thoughts_str = ""
        async for thought in response.thoughts:
            thoughts_str += thought
        if thoughts_str:
            memory.log_conversation_step(agent.conversation_id, "thought", thoughts_str)
            
        output_content = ""
        async for chunk in response:
            output_content += chunk
        if output_content:
            memory.log_conversation_step(agent.conversation_id, "assistant", output_content)
    except Exception:
        active_agents.pop("default", None)

async def run_scheduler():
    await asyncio.sleep(2)
    while True:
        try:
            # Automatic once-a-day database and memory compaction
            try:
                mem = memory.load_memory()
                last_comp = mem.get("key_value", {}).get("last_compaction")
                should_compact = False
                if not last_comp:
                    should_compact = True
                else:
                    last_comp_dt = datetime.fromisoformat(last_comp)
                    if last_comp_dt.tzinfo is None:
                        last_comp_dt = last_comp_dt.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - last_comp_dt).total_seconds() > 86400:  # 24 hours
                        should_compact = True
                        
                if should_compact:
                    loop = asyncio.get_running_loop()
                    stats = await loop.run_in_executor(None, memory.compact_all_memories)
                    print(f"[COMPACTION] Once-a-day background memory compaction complete: {stats}")
            except Exception as e:
                print(f"Error checking/running daily compaction: {e}")

            now_str = datetime.now(timezone.utc).isoformat()
            conn = sqlite3.connect(memory.DB_FILE_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name, prompt, cron_expr FROM scheduled_tasks WHERE status = 'active' AND next_run <= ?",
                (now_str,)
            )
            rows = cursor.fetchall()
            conn.close()
            
            for row in rows:
                task_id, name, prompt, cron_expr = row
                last_run_dt = datetime.now(timezone.utc)
                next_run_dt = get_next_cron_run(cron_expr, last_run_dt)
                memory.update_scheduled_task_run(task_id, last_run_dt.isoformat(), next_run_dt.isoformat())
                asyncio.create_task(execute_scheduled_task(name, prompt))
        except Exception:
            pass
        await asyncio.sleep(5)

# Mount static files directory
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

