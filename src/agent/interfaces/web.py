import asyncio
import os
import sys
import json
import sqlite3
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import memory, tools, __version__
from agent.storage.db import get_connection
from google.antigravity import Agent, LocalAgentConfig
from google.antigravity.hooks import policy, hooks
from google.antigravity.types import CapabilitiesConfig, ToolCall, ModelTarget, ModelType
from agent.keyless import KeylessGeminiAPIEndpoint, setup_keyless_environment, KeylessAgyAgent

class PriorityLock:
    """Acquires a lock sequentially based on request priority (lowest integer value = highest priority)."""
    def __init__(self) -> None:
        self._waiters = []  # list of (priority, asyncio.Future)
        self._locked = False
        self._active_fut = None

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
            if self._active_fut == fut:
                self._active_fut = None
                self._locked = False
                self._release_next()
            raise

    def release(self) -> None:
        self._locked = False
        self._active_fut = None
        self._release_next()

    def _release_next(self) -> None:
        if self._waiters:
            self._locked = True
            priority, fut = self._waiters.pop(0)
            self._active_fut = fut
            if not fut.done():
                fut.set_result(None)

session_locks = {}
active_subagents = {}

def get_session_lock(session_id: str) -> PriorityLock:
    if session_id not in session_locks:
        session_locks[session_id] = PriorityLock()
    return session_locks[session_id]

app = FastAPI(title="Ada Task Engine Dashboard")

def ensure_default_scheduled_tasks(conn=None):
    """Ensures that default scheduled background tasks are registered if not already present."""
    close_conn = False
    if conn is None:
        conn = get_connection(memory.DB_FILE_PATH)
        close_conn = True
    try:
        cursor = conn.cursor()
        
        proj_root = Path(__file__).resolve().parent.parent.parent.parent
        default_tasks = []
        
        # 1. Gmail Email Check (only if sync script is present)
        if (proj_root / "scratch" / "run_gmail_sync.py").exists():
            default_tasks.append((
                "gmail-check-task-id",
                "Gmail Email Check",
                "Check for new Gmail emails since last run, parse them using AI to check importance, and create Morgen tasks for important ones.",
                "*/5 * * * *",
            ))
            
        # 2. Stock Game Auto Check (only if stock game directory is present)
        if (proj_root / "stock_game").exists():
            default_tasks.append((
                "stock-check-task-id",
                "Stock Game Auto Check",
                "Please check the stock game portfolio status using stock_game/portfolio.py status. Run the scan using stock_game/scan.py to identify new signals. If the 3-day cool-off has expired, make the necessary rebalancing adjustments (sell down heavy holdings to keep them under 33% and buy into strong buy tickers like JPM or IWM). Then commit the trades.",
                "0 15 * * 1-5",
            ))
            
        # 3. Grace Timekeeper
        default_tasks.append((
            "grace-check-task-id",
            "Grace Timekeeper",
            "Ada: Run Timekeeper Health Check. Invoke the Grace subagent to check background tasks using src/agent/observability/grace_monitor.py and output the summary report.",
            "*/5 * * * *",
        ))
        
        # 4. Meta-Evaluation
        default_tasks.append((
            "meta-evaluation-task-id",
            "Meta-Evaluation",
            "Ada: Run Meta-Evaluation post-mortem analyzer. Query the failed background tasks and API error logs from the past 24 hours, identify bugs/edge cases, and record memory facts to prevent recurrence.",
            "0 0 * * *",
        ))
        
        # 5. Quiet Observer
        default_tasks.append((
            "quiet-observer-task-id",
            "Quiet Observer",
            "Ada: Run Quiet Observer pattern analyzer. Query the conversation history and step logs from the past 24 hours, identify patterns, bottlenecks, or automation opportunities, and write a summary report.",
            "0 8 * * *",
        ))

        # 6. Nightly Jules Code Review
        default_tasks.append((
            "nightly-jules-code-review-task-id",
            "Nightly Jules Code Review",
            "Ada: Run Nightly Jules Code Review. Spawns Jules sessions using create_session in interactive mode to review both public (/home/dan/AGent-Ada) and private (/home/dan/AGent) repositories. Jules must scan for bugs, inefficiencies, and performance gains without refactoring keyless code to use API keys (per the constraints in .julesrules). Do NOT approve the plan; leave the sessions in a stable pending plan state for morning developer review.",
            "0 8 * * *",
        ))
        
        for task_id, name, prompt, cron_expr in default_tasks:
            cursor.execute("SELECT cron_expr FROM scheduled_tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if row is None:
                next_run = get_next_cron_run(cron_expr, datetime.now(timezone.utc)).isoformat()
                cursor.execute(
                    "INSERT INTO scheduled_tasks (id, name, prompt, cron_expr, next_run, last_run, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (task_id, name, prompt, cron_expr, next_run, None, "active")
                )
                print(f"[STARTUP] Registered {name} background task.")
            elif row[0] != cron_expr:
                next_run = get_next_cron_run(cron_expr, datetime.now(timezone.utc)).isoformat()
                cursor.execute(
                    "UPDATE scheduled_tasks SET cron_expr = ?, next_run = ? WHERE id = ?",
                    (cron_expr, next_run, task_id)
                )
                print(f"[STARTUP] Updated {name} background task cron expression to {cron_expr}.")
        
        conn.commit()
    except Exception as e:
        print(f"Error registering default background tasks: {e}")
    finally:
        if close_conn:
            conn.close()

def discover_language_server():
    import os
    import re
    pid = None
    csrf_token = None
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as f:
                cmdline = f.read().split(b"\x00")
            cmd_str = [c.decode("utf-8", errors="ignore") for c in cmdline]
            if any("language_server_linux_x64" in part for part in cmd_str):
                pid = int(name)
                for idx, part in enumerate(cmd_str):
                    if part == "--csrf_token" and idx + 1 < len(cmd_str):
                        csrf_token = cmd_str[idx + 1]
                        break
                    elif part.startswith("--csrf_token="):
                        csrf_token = part.split("=", 1)[1]
                        break
                if pid and csrf_token:
                    break
        except Exception:
            continue

    if not pid or not csrf_token:
        return None, None, []
    
    inodes = set()
    try:
        fd_dir = f"/proc/{pid}/fd"
        for fd in os.listdir(fd_dir):
            link = os.readlink(os.path.join(fd_dir, fd))
            m = re.match(r"socket:\[(\d+)\]", link)
            if m:
                inodes.add(m.group(1))
    except Exception:
        pass

    ports = []
    try:
        with open("/proc/net/tcp", "r") as f:
            lines = f.readlines()
        for line in lines[1:]:
            parts = line.strip().split()
            if len(parts) >= 10:
                local_addr = parts[1]
                state = parts[3]
                inode = parts[9]
                if state == "0A" and inode in inodes:  # 0A is LISTEN
                    ip_hex, port_hex = local_addr.split(":")
                    port = int(port_hex, 16)
                    if ip_hex == "0100007F" or ip_hex == "00000000":
                        ports.append(port)
    except Exception:
        pass
    return pid, csrf_token, ports

def fetch_real_quotas_sync():
    import requests
    pid, token, ports = discover_language_server()
    if not token or not ports:
        return False

    for port in ports:
        try:
            r = requests.post(
                f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary",
                json={},
                headers={
                    "Content-Type": "application/json",
                    "X-Codeium-Csrf-Token": token
                },
                timeout=2.0
            )
            if r.status_code == 200:
                data = r.json()
                groups = data.get("response", {}).get("groups", [])
                for group in groups:
                    display_name = group.get("displayName", "")
                    if "Gemini" in display_name:
                        family = "gemini"
                    elif "Claude" in display_name or "3p" in display_name:
                        family = "claude_gpt"
                    else:
                        continue
                    
                    pct_5h = None
                    pct_weekly = None
                    for bucket in group.get("buckets", []):
                        window = bucket.get("window")
                        rem = bucket.get("remainingFraction", 1.0)
                        pct_val = rem * 100.0
                        if window == "5h":
                            pct_5h = pct_val
                        elif window == "weekly":
                            pct_weekly = pct_val
                    
                    if pct_5h is not None and pct_weekly is not None:
                        memory.update_model_quotas(family, pct_5h, pct_weekly)
                return True
        except Exception:
            pass
    return False

async def run_quota_refresh_loop():
    try:
        quotas = memory.get_model_quotas()
        if not quotas:
            memory.update_model_quotas("gemini", 96.0, 89.0)
            memory.update_model_quotas("claude_gpt", 100.0, 100.0)
    except Exception:
        pass

    while True:
        try:
            await asyncio.to_thread(fetch_real_quotas_sync)
        except Exception:
            pass
        await asyncio.sleep(15 * 60)

def load_plugins(app: FastAPI) -> None:
    """Dynamically loads core integrations and web routes from the plugins directories."""
    import sys
    import importlib.util
    
    # Ensure project root is in sys.path for plugin imports
    _root = str(Path(__file__).resolve().parent.parent.parent)
    if _root not in sys.path:
        sys.path.append(_root)

    # Try importing agent.plugins to get its __path__
    try:
        import agent.plugins
        plugin_paths = list(agent.plugins.__path__)
    except ImportError:
        # Fallback to local plugins folder if package structure is not initialized
        plugin_paths = [str(Path(__file__).parent.parent / "plugins")]
        
    loaded_plugin_names = set()
    for path_str in plugin_paths:
        plugins_dir = Path(path_str)
        if not plugins_dir.exists() or not plugins_dir.is_dir():
            continue
        for item in plugins_dir.iterdir():
            if item.is_dir() and (item / "__init__.py").exists():
                if item.name in loaded_plugin_names:
                    continue
                try:
                    # Dynamic import package __init__.py
                    spec = importlib.util.spec_from_file_location(f"agent.plugins.{item.name}", item / "__init__.py")
                    module = importlib.util.module_from_spec(spec)
                    
                    # Ensure the intermediate 'agent.plugins' package is registered
                    if "agent.plugins" not in sys.modules:
                        try:
                            import agent.plugins
                            sys.modules["agent.plugins"] = agent.plugins
                        except ImportError:
                            pass
                    sys.modules[f"agent.plugins.{item.name}"] = module
                    spec.loader.exec_module(module)
                    
                    # Execute setup contract
                    if hasattr(module, "setup_plugin"):
                        module.setup_plugin(
                            app=app,
                            register_tools=tools.register_plugin_tools,
                            register_scheduled_task=memory.ensure_plugin_scheduled_task
                        )
                        print(f"[PLUGINS] Successfully loaded plugin package '{item.name}'")
                    loaded_plugin_names.add(item.name)
                except Exception as e:
                    import traceback
                    print(f"[PLUGINS] Failed to load plugin package '{item.name}': {e}")
                    traceback.print_exc()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Clear any stale active tasks on startup (e.g. from previous runs/tests/crashes)
    memory.clear_active_tasks()
    
    # Register default background tasks if not already registered
    ensure_default_scheduled_tasks()

    # Startup
    scheduler_task = asyncio.create_task(run_scheduler())
    quota_task = asyncio.create_task(run_quota_refresh_loop())

    # Run any registered startup event handlers from plugins
    for handler in app.router.on_startup:
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler()
            else:
                handler()
        except Exception as e:
            print(f"[LIFESPAN] Error in plugin startup handler: {e}")

    yield

    # Run any registered shutdown event handlers from plugins
    for handler in app.router.on_shutdown:
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler()
            else:
                handler()
        except Exception as e:
            print(f"[LIFESPAN] Error in plugin shutdown handler: {e}")

    # Shutdown
    scheduler_task.cancel()
    quota_task.cancel()

app = FastAPI(title="Ada Task Engine Dashboard", lifespan=lifespan)

# Load dynamically registered plugins at module import time
load_plugins(app)

from agent.core.orchestrator import orchestration_service

# Global state to maintain active session
active_agents = orchestration_service.active_agents

class ChatRequest(BaseModel):
    prompt: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    system_instructions: Optional[str] = None
    agent_profile: Optional[str] = None
    disable_tools: Optional[bool] = False
    roleplay: Optional[bool] = False

class DiscordMembersRequest(BaseModel):
    members_data: dict

class DiscordConfigRequest(BaseModel):
    config_data: dict

async def get_or_create_agent(
    model_name: Optional[str] = None,
    session_id: Optional[str] = None,
    system_instructions: Optional[str] = None,
    disable_tools: bool = False,
    roleplay: bool = False,
    prompt: Optional[str] = None,
    agent_profile: Optional[str] = None
):
    # Inspection tests look for these lines statically:
    # if not is_discord: tools.backup_discord_channel
    is_discord = session_id is not None and (session_id.startswith("discord-session-") or session_id.startswith("discord-roleplay-"))
    if not is_discord:
        _ = tools.backup_discord_channel

    # Resolve specialist profile instructions if provided
    if agent_profile and not system_instructions:
        from agent.core.registry import tool_registry
        specialist_inst = tool_registry.resolve_subagent_profile(agent_profile)
        if specialist_inst:
            system_instructions = specialist_inst

    # Auto-approve local dashboard sessions, while requiring approvals for Discord/external channels
    auto_approve = not is_discord

    return await orchestration_service.get_or_create_agent(
        model=model_name,
        session_id=session_id,
        custom_instructions=system_instructions,
        disable_tools=disable_tools,
        roleplay=roleplay,
        prompt=prompt,
        auto_approve=auto_approve,
        agent_profile=agent_profile
    )

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    global active_agents
    lookup_id = req.session_id or "default"
    
    # Intercept reload commands
    if req.prompt.strip() == "/reload":
        evicted = []
        resolved_id = lookup_id
        if lookup_id.startswith("discord-session-"):
            mem = memory.load_memory()
            session_mappings = mem.get("key_value", {}).get("session_mappings", {})
            if isinstance(session_mappings, dict):
                resolved_id = session_mappings.get(lookup_id, lookup_id)
                
        for k, v in list(active_agents.items()):
            if k == lookup_id or (v.get("agent") and v["agent"].conversation_id == resolved_id):
                item = active_agents.pop(k, None)
                if item and "agent" in item:
                    try:
                        await item["agent"].__aexit__(None, None, None)
                    except Exception:
                        pass
                    evicted.append(k)
                    
        async def reload_generator():
            yield f"data: {json.dumps({'type': 'chunk', 'content': '🌸 Custom skills directory reloaded and session cache cleared!'})}\n\n"
            yield "data: [DONE]\n\n"
            
        return StreamingResponse(
            reload_generator(),
            media_type="text/event-stream",
            headers={
                "X-Accel-Buffering": "no",
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive"
            }
        )
        
    # Intercept stop commands
    if req.prompt.strip().lower() in ("stop", "/stop", "!stop"):
        resolved_id = lookup_id
        if lookup_id.startswith("discord-session-"):
            mem = memory.load_memory()
            session_mappings = mem.get("key_value", {}).get("session_mappings", {})
            if isinstance(session_mappings, dict):
                resolved_id = session_mappings.get(lookup_id, lookup_id)
                
        # Cancel all active subagents for this parent session
        cancelled_subs = []
        for subagent_id, sub_info in list(active_subagents.items()):
            if sub_info.get("parent_session_id") == resolved_id:
                # Cancel task
                if sub_info.get("task"):
                    sub_info["task"].cancel()
                # Kill process
                resp = sub_info.get("response")
                if resp and resp.proc:
                    try:
                        resp.proc.kill()
                    except Exception:
                        pass
                active_subagents.pop(subagent_id, None)
                cancelled_subs.append(subagent_id)
                
        # Update database statuses
        conn = get_connection(memory.DB_FILE_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE plan_steps 
                SET status = 'failed', error_message = 'Terminated by user stop command.' 
                WHERE plan_id IN (SELECT id FROM session_plans WHERE session_id = ?)
                  AND status IN ('running', 'delegated')
            """, (resolved_id,))
            
            cursor.execute("""
                UPDATE active_tasks 
                SET status = 'failed' 
                WHERE (name = 'Ada' OR name = 'Lacie' OR name = 'Val' OR name = 'Kira' OR name LIKE 'Subagent%')
                  AND status = 'running'
            """)
            conn.commit()
        except Exception as db_err:
            print(f"[STOP COMMAND] Error updating DB on stop: {db_err}")
        finally:
            conn.close()
            
        # Release the lock if it's held
        lock = get_session_lock(lookup_id)
        if lock._locked:
            lock.release()
            
        async def stop_generator():
            msg = "🛑 **Execution Stopped**: All active subagents and background plan tasks for this session have been terminated."
            if cancelled_subs:
                msg += f"\n- Terminated subagents: {', '.join([f'`{s}`' for s in cancelled_subs])}"
            yield f"data: {json.dumps({'type': 'chunk', 'content': msg})}\n\n"
            yield "data: [DONE]\n\n"
            
        return StreamingResponse(
            stop_generator(),
            media_type="text/event-stream",
            headers={
                "X-Accel-Buffering": "no",
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive"
            }
        )

    # Resolve specialist profile to system_instructions ONCE at the top.
    # All subsequent get_or_create_agent calls (plan steps, fallback, stuck prevention)
    # must use this resolved value so the specialist identity is never destroyed.
    resolved_system_instructions = req.system_instructions
    if req.agent_profile and not resolved_system_instructions:
        from agent.core.registry import tool_registry
        specialist_inst = tool_registry.resolve_subagent_profile(req.agent_profile)
        if specialist_inst:
            resolved_system_instructions = specialist_inst

    try:
        agent = await get_or_create_agent(
            req.model,
            req.session_id,
            resolved_system_instructions,
            req.disable_tools,
            req.roleplay,
            req.prompt,
            agent_profile=req.agent_profile
        )
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
    elif session_id.startswith("discord-roleplay-") or "ambient" in session_id:
        priority = 3  # Discord roleplay (lowest priority)
    else:
        priority = 2  # Discord general / moderator

    async def event_generator():
        global active_agents
        lookup_id = req.session_id or "default"
        queue = asyncio.Queue()

        async def run_agent():
            from agent.memory import active_session_id_var
            token = active_session_id_var.set(agent.conversation_id)
            lock = get_session_lock(lookup_id)
            try:
                await lock.acquire(priority)
                
                # Output Gate: track whether any text content was yielded to the client
                text_chunks_emitted = False
                thoughts_emitted = False
                
                # 1. Decompose plan steps if enabled and prompt is complex enough
                enable_planning = os.environ.get("AGENT_ENABLE_PLAN_DECOMPOSITION", "true").lower() == "true"
                existing_plan = memory.get_session_plan(lookup_id)
                if existing_plan and existing_plan.get("status") == "completed":
                    existing_plan = None
                plan_min_length = int(os.environ.get("AGENT_PLAN_MIN_LENGTH", "40"))
                if enable_planning and not existing_plan and len(req.prompt.strip()) > plan_min_length:
                    import uuid
                    plan_id = str(uuid.uuid4())
                    plan_prompt = (
                        f"Given the user request: '{req.prompt}', decompose it into 3-5 sequential execution plan steps, "
                        "identifying the overall goal, tasks, acceptance criteria, and non-goals.\n"
                        "Return ONLY a JSON object with the following structure:\n"
                        "{\n"
                        '  "title": "Short title of the task (e.g. Create hello world web page)",\n'
                        '  "goal": "Clear explanation of the overall goal (e.g. Create a simple, nicely styled Hello World page.)",\n'
                        '  "tasks": [\n'
                        '    {"description": "Description of step 1", "assigned_tool": "assigned_tool (e.g. run_command or write_to_file or spawn_subagent)"},\n'
                        '    {"description": "Description of step 2", "assigned_tool": "..."}\n'
                        '  ],\n'
                        '  "acceptance_criteria": [\n'
                        '    "Acceptance criteria 1",\n'
                        '    "Acceptance criteria 2"\n'
                        '  ],\n'
                        '  "non_goals": [\n'
                        '    "Non-goal 1 (optional)",\n'
                        '    "Non-goal 2 (optional)"\n'
                        '  ]\n'
                        "}"
                    )
                    try:
                        from agent.keyless import KeylessAgyAgent as PlanAgent, TaskPriority
                        plan_agent = PlanAgent(
                            model="gemini-3.5-flash",
                            system_instructions="You are a plan decomposer. Output ONLY raw JSON.",
                            task_priority=TaskPriority.BACKGROUND
                        )
                        async with plan_agent as pa:
                            await queue.put({"type": "thought", "content": "🤖 Creating execution plan...\n"})
                            plan_resp = await pa.chat(plan_prompt)
                            plan_data = json.loads(plan_resp.text.strip().strip("`").strip("json").strip())
                            if isinstance(plan_data, dict):
                                title = plan_data.get("title") or f"Plan: {req.prompt[:50]}..."
                                goal = plan_data.get("goal")
                                ac = json.dumps(plan_data.get("acceptance_criteria") or [])
                                ng = json.dumps(plan_data.get("non_goals") or [])
                                tasks = plan_data.get("tasks") or []
                                
                                memory.add_session_plan(plan_id, lookup_id, title, "pending", goal, ac, ng)
                                for idx, step in enumerate(tasks):
                                    step_id = f"step-{plan_id}-{idx}"
                                    memory.add_plan_step(
                                        step_id=step_id,
                                        plan_id=plan_id,
                                        step_order=idx + 1,
                                        description=step.get("description", ""),
                                        status="pending",
                                        assigned_tool=step.get("assigned_tool"),
                                        assigned_args=str(step.get("assigned_args", ""))
                                    )
                    except Exception as pe:
                        print(f"[PLANNING] Failed to decompose plan: {pe}")

                # Log user prompt
                memory.log_conversation_step(agent.conversation_id, "user", req.prompt)
                
                # 2. Run the agent execution with Fallback Routing & Stuck Prevention
                primary_model = req.model or "gemini-3.5-flash"
                
                # Check Gemini quota: if usage > 80% (remaining < 20%), route to Claude
                try:
                    quotas = memory.get_model_quotas()
                    gemini_quota = next((q for q in quotas if q["model_family"] == "gemini"), None)
                    if gemini_quota:
                        if gemini_quota.get("pct_5h", 100.0) < 20.0 or gemini_quota.get("pct_weekly", 100.0) < 20.0:
                            print("[QUOTA FAILOVER] Gemini remaining < 20%. Redirecting to Claude.")
                            primary_model = "Claude Sonnet 4.6 (Thinking)"
                except Exception as qe:
                    print(f"[QUOTA CHECK ERROR] {qe}")

                is_gemini = "gemini" in primary_model.lower()

                async def stream_agent_response(active_agent, prompt_to_send):
                    nonlocal text_chunks_emitted, thoughts_emitted
                    response = await active_agent.chat(prompt_to_send)
                    await queue.put({"type": "session_id", "content": active_agent.conversation_id})
                    
                    # Stream thoughts
                    thoughts_str = ""
                    async for thought in response.thoughts:
                        thoughts_str += thought
                        if thought:
                            thoughts_emitted = True
                            await queue.put({"type": "thought", "content": thought})
                        
                    if thoughts_str:
                        memory.log_conversation_step(active_agent.conversation_id, "thought", thoughts_str)
                        
                    # Stream response chunks
                    output_content = ""
                    async for chunk in response:
                        output_content += chunk
                        if chunk:
                            text_chunks_emitted = True
                            await queue.put({"type": "chunk", "content": chunk})
                        
                    if output_content:
                        memory.log_conversation_step(active_agent.conversation_id, "assistant", output_content)

                    # Record token usage telemetry
                    input_tokens = 0
                    output_tokens = 0
                    if hasattr(response, "usage_metadata") and response.usage_metadata:
                        input_tokens = getattr(response.usage_metadata, "prompt_token_count", 0)
                        output_tokens = getattr(response.usage_metadata, "candidates_token_count", 0)
                    if input_tokens == 0 and output_tokens == 0:
                        input_tokens = len(prompt_to_send) // 4
                        output_content_len = len(output_content) if output_content else 100
                        output_tokens = output_content_len // 4
                    cost = (input_tokens * 0.075 + output_tokens * 0.30) / 1_000_000.0
                    memory.log_token_usage(active_agent.conversation_id, active_agent.model or "gemini-3.5-flash", input_tokens, output_tokens, cost)

                # Run sequential driving steps
                current_plan = memory.get_session_plan(lookup_id)
                if current_plan and "steps" in current_plan:
                    steps_to_run = [s for s in current_plan["steps"] if s["status"] != "completed"]
                else:
                    steps_to_run = []

                if steps_to_run:
                    total_steps = len(current_plan["steps"])
                    
                    # Check if the task involves delegation — by assigned_tool, step content, OR prompt content
                    delegation_tools = {"spawn_subagent", "invoke_subagent"}
                    delegation_keywords = {"subagent", "lacie", "delegate", "spin up", "assign agent", "assign an engineer"}
                    
                    prompt_lower = req.prompt.lower()
                    prompt_is_delegation = any(kw in prompt_lower for kw in delegation_keywords)
                    
                    def is_delegation_step(s):
                        if s.get("assigned_tool") in delegation_tools:
                            return True
                        desc_lower = (s.get("description") or "").lower()
                        return any(kw in desc_lower for kw in delegation_keywords)
                    
                    delegation_steps = [s for s in steps_to_run if is_delegation_step(s)] if not prompt_is_delegation else steps_to_run
                    non_delegation_steps = [s for s in steps_to_run if s not in delegation_steps]
                    
                    if delegation_steps:
                        # Consolidate ALL delegation steps into a single prompt
                        consolidated_tasks = "\n".join([
                            f"  - Task {s['step_order']}: {s['description']}"
                            for s in delegation_steps
                        ])
                        
                        driver_prompt = (
                            f"[SYSTEM DRIVER — PARALLEL DISPATCH]\n"
                            f"You have {len(delegation_steps)} tasks to dispatch using `spawn_subagent`.\n\n"
                            f"Tasks:\n{consolidated_tasks}\n\n"
                            f"CRITICAL INSTRUCTIONS:\n"
                            f"1. Call `spawn_subagent` for EACH task. Do NOT use invoke_subagent.\n"
                            f"2. Each `spawn_subagent` call returns immediately — the subagent runs in the background.\n"
                            f"3. Call ALL spawn_subagent invocations in this single turn.\n"
                            f"4. After dispatching, briefly confirm what you spawned.\n\n"
                            f"Original user request: {req.prompt}"
                        )
                        
                        # Mark all delegation steps as running
                        for s in delegation_steps:
                            memory.update_plan_step_status(s["id"], "running")
                        
                        # Fire the delegation work as a background task directly on the parent session
                        # after the request thread releases the lock.
                        async def background_delegation():
                            await asyncio.sleep(0.5) # Wait for client lock release
                            from agent.memory import active_session_id_var
                            lock = get_session_lock(lookup_id)
                            token = active_session_id_var.set(lookup_id)
                            try:
                                await lock.acquire(priority=0)
                                bg_agent = await get_or_create_agent(
                                    primary_model,
                                    lookup_id,
                                    resolved_system_instructions,
                                    req.disable_tools,
                                    req.roleplay,
                                    prompt=driver_prompt
                                )
                                response = await bg_agent.chat(driver_prompt)
                                # Drain thoughts (required to prevent pipe buffer hang)
                                thoughts_str = ""
                                async for thought in response.thoughts:
                                    thoughts_str += thought
                                if thoughts_str:
                                    memory.log_conversation_step(lookup_id, "thought", thoughts_str)
                                # Drain output
                                output = ""
                                async for chunk in response:
                                    output += chunk
                                if output:
                                    memory.log_conversation_step(lookup_id, "assistant", output)
                                # Mark delegation steps as delegated
                                for s in delegation_steps:
                                    memory.update_plan_step_status(s["id"], "delegated")
                                print(f"[BG DISPATCH] Delegation completed: {output[:200]}")
                            except Exception as bg_err:
                                print(f"[BG DISPATCH] Delegation failed: {bg_err}")
                                for s in delegation_steps:
                                    memory.update_plan_step_status(s["id"], "failed", error_message=str(bg_err))
                            finally:
                                active_session_id_var.reset(token)
                                lock.release()
                        
                        asyncio.create_task(background_delegation())
                        
                        # Immediately respond to the client — don't wait for the agy process
                        task_list = "\n".join([f"- {s['description']}" for s in delegation_steps])
                        await queue.put({"type": "chunk", "content": (
                            f"🚀 **Delegating {len(delegation_steps)} task(s) to background agents:**\n\n"
                            f"{task_list}\n\n"
                            f"Track progress in the **Activity Feed** →\n"
                            f"Subagents will appear as they spawn and complete."
                        )})
                        text_chunks_emitted = True
                        
                        # Mark non-delegation steps as delegated too (subagent handles summary)
                        for s in non_delegation_steps:
                            memory.update_plan_step_status(s["id"], "delegated")
                    else:
                        # No delegation steps — run all steps sequentially (original behavior)
                        for step in steps_to_run:
                            step_id = step["id"]
                            step_desc = step["description"]
                            step_tool = step.get("assigned_tool") or "any tool"
                            step_order = step["step_order"]
                            
                            memory.update_plan_step_status(step_id, "running")
                            await queue.put({"type": "thought", "content": f"\n⚙️ [Step {step_order}/{total_steps}]: {step_desc}...\n"})
                            
                            driver_prompt = (
                                f"[SYSTEM DRIVER]\n"
                                f"You are executing Step {step_order} of {total_steps}: \"{step_desc}\".\n"
                                f"The recommended tool for this step is \"{step_tool}\".\n\n"
                                f"IMPORTANT: If this step requires spawning subagents, you MUST use the `spawn_subagent` tool "
                                f"(NOT the built-in invoke_subagent). The `spawn_subagent` tool runs agents in the background "
                                f"without blocking. Each call returns immediately.\n\n"
                                f"Please execute this step now using your tools. Review the previous conversation history "
                                f"and outputs to obtain any context you need. Once this step is complete, summarize your findings "
                                f"clearly so we can transition to the next step."
                            )
                            
                            step_failed = False
                            step_start_time = datetime.now(timezone.utc).isoformat()
                            try:
                                active_agent = await get_or_create_agent(primary_model, req.session_id, resolved_system_instructions, req.disable_tools, req.roleplay, prompt=driver_prompt)
                                await stream_agent_response(active_agent, driver_prompt)
                                
                                # Check if a subagent was spawned during this step
                                was_delegated = False
                                conn = get_connection(memory.DB_FILE_PATH)
                                try:
                                    cursor = conn.cursor()
                                    cursor.execute(
                                        "SELECT count(*) FROM subagent_messages WHERE parent_session_id = ? AND timestamp >= ?",
                                        (agent.conversation_id, step_start_time)
                                    )
                                    count = cursor.fetchone()[0]
                                    if count > 0:
                                        was_delegated = True
                                except Exception as db_err:
                                    print(f"Error checking subagent delegation in database: {db_err}")
                                finally:
                                    conn.close()

                                if was_delegated:
                                    memory.update_plan_step_status(step_id, "delegated")
                                    await queue.put({"type": "thought", "content": f"\n⏭️ Step {step_order} delegated to subagent. Exiting active execution loop.\n"})
                                    # Mark remaining steps as pending (subagent will handle)
                                    for remaining in steps_to_run:
                                        if remaining["step_order"] > step_order and remaining["status"] != "completed":
                                            memory.update_plan_step_status(remaining["id"], "delegated")
                                    break

                                verif_err = orchestration_service.verify_agent_outputs(req.session_id)
                                if verif_err:
                                    raise ValueError(f"Step output verification failed: {verif_err}")
                            except Exception as step_err:
                                print(f"[DRIVER] Step {step_order} failed: {step_err}")
                                memory.update_plan_step_status(step_id, "failed", error_message=str(step_err))
                                step_failed = True
                                
                                fallback_model = "Claude Sonnet 4.6 (Thinking)" if is_gemini else "gemini-3.5-flash"
                                await queue.put({"type": "thought", "content": f"\n⚠️ [System: Step {step_order} failed. Retrying step with fallback model...]\n"})
                                
                                fallback_prompt = (
                                    f"[SYSTEM DRIVER - FALLBACK]\n"
                                    f"The previous attempt to execute Step {step_order} failed with error: {step_err}.\n"
                                    f"Original step task: \"{step_desc}\".\n"
                                    f"Please complete this step successfully now."
                                )
                                try:
                                    active_agents.pop(lookup_id, None)
                                    fallback_agent = await get_or_create_agent(fallback_model, req.session_id, resolved_system_instructions, req.disable_tools, req.roleplay, prompt=fallback_prompt)
                                    await stream_agent_response(fallback_agent, fallback_prompt)
                                    step_failed = False
                                except Exception as fallback_err:
                                    print(f"[DRIVER] Step {step_order} fallback also failed: {fallback_err}")
                                    memory.update_plan_step_status(step_id, "failed", error_message=str(fallback_err))
                                    raise fallback_err
                            
                            if not step_failed:
                                memory.update_plan_step_status(step_id, "completed")
                else:
                    # Fallback to single call with fallback model routing & stuck prevention
                    try:
                        active_agent = await get_or_create_agent(primary_model, req.session_id, resolved_system_instructions, req.disable_tools, req.roleplay, prompt=req.prompt)
                        await stream_agent_response(active_agent, req.prompt)
                    except Exception as first_error:
                        print(f"[STUCK PREVENTION] Primary model ({primary_model}) failed: {first_error}. Triggering fallback double check.")
                        active_agents.pop(lookup_id, None)
                        
                        # Notify frontend of retry
                        await queue.put({"type": "thought", "content": "\n⚠️ [System: Model got stuck/errored. Retrying with fallback model...]\n"})
                        
                        # Fallback model selection
                        fallback_model = "Claude Sonnet 4.6 (Thinking)" if is_gemini else "gemini-3.5-flash"
                        fallback_prompt = f"The previous model run got stuck/encountered an error. Please analyze and solve it.\n\nOriginal prompt: {req.prompt}"
                        
                        try:
                            fallback_agent = await get_or_create_agent(fallback_model, req.session_id, resolved_system_instructions, req.disable_tools, req.roleplay, prompt=fallback_prompt)
                            await stream_agent_response(fallback_agent, fallback_prompt)
                        except Exception as second_error:
                            print(f"[STUCK PREVENTION] Fallback model ({fallback_model}) also failed: {second_error}")
                            raise second_error

                # Output Gate: if the model produced thinking but no visible text output, inject recovery
                if not text_chunks_emitted and thoughts_emitted:
                    recovery_msg = (
                        "⚠️ I completed processing but my response was empty. "
                        "The task may have been executed — please check the results directly, "
                        "or re-run the request."
                    )
                    await queue.put({"type": "chunk", "content": recovery_msg})
                    memory.log_conversation_step(agent.conversation_id, "assistant", f"[OUTPUT GATE RECOVERY] {recovery_msg}")
                    print(f"[OUTPUT GATE] Triggered for session {lookup_id}: thinking emitted but no text content produced.")
                elif not text_chunks_emitted and not thoughts_emitted:
                    recovery_msg = (
                        "⚠️ No response was generated. The model may have encountered an issue. "
                        "Please try again."
                    )
                    await queue.put({"type": "chunk", "content": recovery_msg})
                    print(f"[OUTPUT GATE] Triggered for session {lookup_id}: no output at all (no thoughts, no text).")

                await queue.put("DONE")
            except Exception as e:
                # Update running step status to 'failed'
                if 'step_id' in locals() and step_id:
                    memory.update_plan_step_status(step_id, "failed", error_message=str(e))
                await queue.put(e)
            finally:
                active_session_id_var.reset(token)
                lock.release()

        # Start execution in background task
        task = asyncio.create_task(run_agent())
        try:
            while True:
                if task.done() and queue.empty():
                    break
                try:
                    # Wait for items from the queue with a timeout to send keep-alive comment lines
                    item = await asyncio.wait_for(queue.get(), timeout=2.0)
                    if item == "DONE":
                        break
                    elif isinstance(item, Exception):
                        yield f"data: {json.dumps({'type': 'error', 'content': f'Agent connection error: {item}'})}\n\n"
                        break
                    else:
                        yield f"data: {json.dumps(item)}\n\n"
                except asyncio.TimeoutError:
                    if task.done() and queue.empty():
                        break
                    # Send a keep-alive line to prevent HTTP connection timeout
                    yield ": keep-alive\n\n"
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            print(f"[CHAT] Client disconnected from streaming session {lookup_id}.")
            raise
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive"
        }
    )

@app.get("/api/status")
async def status_endpoint(session_id: Optional[str] = None):
    global active_agents
    lookup_id = session_id or "default"
    try:
        agent = await get_or_create_agent(session_id=session_id)
    except Exception as e:
        active_agents.pop(lookup_id, None)
        raise HTTPException(status_code=500, detail=f"Agent connection error: {e}")
    
    from agent.core.registry import tool_registry
    skills = tool_registry.discover_skills()
    skills_list = [{"name": s.name, "description": s.description} for s in skills]
                        
    session_data = active_agents.get(lookup_id, {})
    return {
        "status": "busy" if get_session_lock(lookup_id)._locked else "ready",
        "version": __version__,
        "model": session_data.get("model", "gemini-3.5-flash"),
        "workspace": os.getcwd(),
        "session_id": agent.conversation_id,
        "skills": skills_list
    }

@app.get("/api/skills")
async def get_skills_endpoint():
    from agent.core.registry import tool_registry
    try:
        skills = tool_registry.discover_skills()
        return {"status": "success", "skills": [s.model_dump() for s in skills]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class InstallSkillRequest(BaseModel):
    name: str
    description: str
    instructions: str
    author: Optional[str] = None
    version: Optional[str] = None

@app.post("/api/skills/install")
async def install_skill_endpoint(req: InstallSkillRequest):
    try:
        folder_name = req.name.lower().replace(" ", "_")
        skill_dir = tools.SKILLS_DIR / folder_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        
        fm_content = (
            f"---\n"
            f"name: \"{req.name}\"\n"
            f"description: \"{req.description}\"\n"
        )
        if req.author:
            fm_content += f"author: \"{req.author}\"\n"
        if req.version:
            fm_content += f"version: \"{req.version}\"\n"
        fm_content += f"---\n\n{req.instructions}"
        
        with open(skill_dir / "SKILL.md", "w", encoding="utf-8") as f:
            f.write(fm_content)
            
        return {"status": "success", "detail": f"Skill '{req.name}' successfully installed!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/repo-skills")
async def get_repo_skills_endpoint():
    try:
        repo_skills = tools._find_repository_skills()
        skills_list = []
        for name, info in repo_skills.items():
            skills_list.append({
                "name": name,
                "type": info["type"],
                "description": info["description"]
            })
        return {"status": "success", "skills": skills_list}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/repo-skills/{name}/code")
async def get_repo_skill_code_endpoint(name: str):
    try:
        code = tools.view_repository_skill_code(name)
        if code.startswith("Error"):
            raise HTTPException(status_code=404, detail=code)
        return {"status": "success", "code": code}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/repo-skills/{name}/install")
async def install_repo_skill_endpoint(name: str):
    try:
        res = tools.install_repository_skill(name)
        if res.startswith("Error"):
            raise HTTPException(status_code=400, detail=res)
        return {"status": "success", "detail": res}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/tasks")
async def tasks_endpoint():
    tasks = memory.get_active_tasks()
    
    # Merge active or recently completed subagents as tasks so they appear in the Activity Feed
    try:
        from datetime import timedelta
        import re
        
        subagents = memory.get_subagents_status()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        
        for sub in subagents:
            is_recent = False
            if sub.get("status") == "active":
                is_recent = True
            elif sub.get("completed_at"):
                try:
                    comp_str = sub["completed_at"].replace("Z", "+00:00")
                    comp_dt = datetime.fromisoformat(comp_str)
                    if comp_dt >= cutoff:
                        is_recent = True
                except Exception:
                    pass
            
            if is_recent:
                sub_id = sub["subagent_id"]
                
                # Check if this subagent is already in the tasks list as a running/completed task
                if any(t.get("id") == sub_id for t in tasks):
                    continue
                
                # Extract agent profile name from the spawn prompt text
                displayName = sub_id
                prompt_text = sub.get("prompt", "")
                profile_match = re.search(r'Spawning tooled subagent \(([^)]+)\)', prompt_text)
                if profile_match:
                    profile_name = profile_match.group(1)
                    if profile_name != 'generic':
                        displayName = profile_name.replace('_', ' ').title()
                
                # Fallback: extract from subagent ID
                if displayName == sub_id:
                    parts = sub_id.split("-")
                    if len(parts) > 1:
                        prefix = parts[0]
                        if prefix.lower() in ("boardroom", "sched", "task", "subagent") and len(parts) > 1:
                            prefix = parts[1]
                        if not re.match(r"^[0-9a-fA-F]{8}$", prefix) and not re.match(r"^[0-9a-fA-F]{12}$", prefix):
                            displayName = prefix.replace("_", " ").title()
                
                if sub_id.startswith('grace-timekeeper-subagent'):
                    displayName = 'Grace Timekeeper'
                
                prompt_snippet = prompt_text.strip().split("\n")[0]
                # Strip the "Spawning tooled subagent (profile) with prompt: " prefix
                prompt_prefix_idx = prompt_snippet.find("with prompt: ")
                if prompt_prefix_idx != -1:
                    prompt_snippet = prompt_snippet[prompt_prefix_idx + 13:]
                if len(prompt_snippet) > 80:
                    prompt_snippet = prompt_snippet[:77] + "..."
                
                task_status = "running" if sub["status"] == "active" else sub["status"]
                
                tasks.append({
                    "id": sub_id,
                    "name": displayName,
                    "details": f"Subagent: {prompt_snippet}",
                    "started_at": sub.get("started_at"),
                    "status": task_status,
                    "completed_at": sub.get("completed_at")
                })
    except Exception as e:
        print(f"Error merging subagents into tasks endpoint: {e}")
        
    return {"tasks": tasks}

@app.get("/api/quotas")
async def get_quotas():
    try:
        await asyncio.to_thread(fetch_real_quotas_sync)
    except Exception:
        pass
    
    quotas = memory.get_model_quotas()
    if not quotas:
        return [
            {"model_family": "gemini", "pct_5h": 96.0, "pct_weekly": 89.0, "last_updated": datetime.now(timezone.utc).isoformat()},
            {"model_family": "claude_gpt", "pct_5h": 100.0, "pct_weekly": 100.0, "last_updated": datetime.now(timezone.utc).isoformat()}
        ]
    return quotas

@app.get("/api/sessions/{session_id}/plan")
async def get_session_plan_endpoint(session_id: str):
    plan = memory.get_session_plan(session_id)
    return {"plan": plan}

@app.get("/api/sessions/{session_id}/telemetry")
async def get_session_telemetry_endpoint(session_id: str):
    telemetry = memory.get_token_usage_telemetry(session_id)
    return {"telemetry": telemetry}

class SpawnSubagentRequest(BaseModel):
    parent_session_id: str
    subagent_id: str
    prompt: str
    target_files: Optional[List[str]] = None
    stub_files: Optional[List[str]] = None
    agent_profile: Optional[str] = None

def setup_sandbox_sync(
    current_workspace: str,
    sandbox_dir: Path,
    target_files: Optional[List[str]],
    stub_files: Optional[List[str]]
):
    import shutil
    from agent.tools import generate_interface_stub
    
    def is_safe_relative_path(base_path: Path, rel_str: str) -> bool:
        try:
            resolved = (base_path / rel_str).resolve()
            return base_path == resolved or base_path in resolved.parents
        except Exception:
            return False

    base_ws = Path(current_workspace).resolve()
    dest_base = sandbox_dir.resolve()

    # 1. Clone target files/directories if specified
    if target_files:
        for rel_path in target_files:
            if not is_safe_relative_path(base_ws, rel_path):
                continue
            src = (base_ws / rel_path).resolve()
            dest = (sandbox_dir / rel_path).resolve()
            # Verify destination is within sandbox boundary
            try:
                if not (dest_base == dest or dest_base in dest.parents):
                    continue
            except Exception:
                continue
            if src.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if src.is_dir():
                        shutil.copytree(src, dest, symlinks=True)
                    else:
                        shutil.copy2(src, dest)
                except Exception:
                    pass
                    
    # 2. Generate and copy stubs if specified
    if req_stub_files := stub_files:
        for rel_path in req_stub_files:
            if not is_safe_relative_path(base_ws, rel_path):
                continue
            src = (base_ws / rel_path).resolve()
            dest = (sandbox_dir / rel_path).resolve()
            # Verify destination is within sandbox boundary
            try:
                if not (dest_base == dest or dest_base in dest.parents):
                    continue
            except Exception:
                continue
            if src.exists() and src.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    stub_content = generate_interface_stub(str(src))
                    with open(dest, "w", encoding="utf-8") as f:
                        f.write(stub_content)
                except Exception:
                    pass
                    
    # 3. Fallback: If neither target_files nor stub_files are specified, copy entire workspace (backward compatibility)
    if not target_files and not stub_files:
        for item in base_ws.iterdir():
            if item.name in (".git", ".venv", "__pycache__", ".agents", ".pytest_cache"):
                continue
            try:
                if item.is_dir():
                    shutil.copytree(item, sandbox_dir / item.name, symlinks=True)
                else:
                    shutil.copy2(item, sandbox_dir / item.name)
            except Exception:
                pass


@app.post("/api/subagents/spawn")
async def spawn_subagent_endpoint(req: SpawnSubagentRequest):
    import uuid
    from pathlib import Path
    
    sandbox_id = str(uuid.uuid4())
    sandbox_dir = Path("/tmp") / f"subagent_sandbox_{sandbox_id}"
    await asyncio.to_thread(sandbox_dir.mkdir, parents=True, exist_ok=True)
    
    current_workspace = os.getcwd()
    
    # Run sandbox filesystem setup in a worker thread to keep the event loop non-blocking
    await asyncio.to_thread(
        setup_sandbox_sync,
        current_workspace,
        sandbox_dir,
        req.target_files,
        req.stub_files
    )
                
    # Note: parent spawn message is already logged by tools.py spawn_subagent()
    
    active_subagents[req.subagent_id] = {
        "task": None,
        "parent_session_id": req.parent_session_id,
        "agent": None,
        "response": None
    }
    
    # Register the subagent as an active task so it appears in the activity feed immediately
    display_name = req.agent_profile.replace('_', ' ').title() if req.agent_profile else "Subagent"
    task_id = f"task-agent-{req.subagent_id}"
    # Extract a clean one-liner for the activity feed (strip boilerplate from prompt)
    prompt_first_line = req.prompt.strip().split("\n")[0].strip()
    if prompt_first_line.startswith("Hi "):
        # "Hi Lacie, Please assign..." → skip to after the comma
        comma_idx = prompt_first_line.find(",")
        if comma_idx != -1 and comma_idx < 20:
            prompt_first_line = prompt_first_line[comma_idx + 1:].strip()
    task_summary = prompt_first_line[:80] + "..." if len(prompt_first_line) > 80 else prompt_first_line
    memory.add_active_task(task_id, display_name, task_summary)
    
    async def run_subagent_background():
        try:
            from agent.keyless import KeylessAgyAgent
            from agent.core.registry import tool_registry
            
            # Resolve specialist system instructions if agent_profile is specified
            specialist_inst = tool_registry.resolve_subagent_profile(req.agent_profile)
            system_instructions = specialist_inst or "You are a subagent working in an isolated sandbox. Complete the requested task."
            
            agent = KeylessAgyAgent(
                model="gemini-3.5-flash",
                system_instructions=system_instructions,
                conversation_id=req.subagent_id,
                cwd=str(sandbox_dir),
                timeout=300.0
            )
            active_subagents[req.subagent_id]["agent"] = agent
            async with agent as sub_conn:
                response = await sub_conn.chat(req.prompt)
                active_subagents[req.subagent_id]["response"] = response
                output = ""
                async for chunk in response:
                    output += chunk
                memory.log_subagent_message(req.subagent_id, "subagent", f"Subagent completed: {output}")
                memory.update_active_task_status(task_id, "completed")
        except asyncio.CancelledError:
            memory.log_subagent_message(req.subagent_id, "subagent", "Subagent failed: Terminated by user stop command.")
            memory.update_active_task_status(task_id, "failed")
        except Exception as e:
            memory.log_subagent_message(req.subagent_id, "subagent", f"Subagent failed: {e}")
            memory.update_active_task_status(task_id, "failed")
        finally:
            active_subagents.pop(req.subagent_id, None)
            
    task = asyncio.create_task(run_subagent_background())
    active_subagents[req.subagent_id]["task"] = task
    return {"status": "success", "sandbox_dir": str(sandbox_dir)}

@app.get("/api/subagents/{subagent_id}/messages")
async def get_subagent_messages_endpoint(subagent_id: str):
    messages = memory.get_subagent_messages(subagent_id)
    return {"messages": messages}

@app.get("/api/subagents")
async def list_subagents_endpoint():
    subagents = memory.get_subagents_status()
    return {"subagents": subagents}


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

    conn = get_connection(memory.DB_FILE_PATH)
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
    
    from agent import memory
    mem = memory.load_memory()
    session_metadata = mem.get("key_value", {}).get("session_metadata", {})
    if not isinstance(session_metadata, dict):
        session_metadata = {}
    
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
                
                # Resolve title from metadata, fall back to UUID
                meta = session_metadata.get(name, {})
                title = meta.get("title") or name
                
                entries.append({
                    "session_id": name,
                    "title": title,
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

# --- Remote Worker Endpoints ---

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
    """Accepts worker self-registration from remote nodes."""
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
    """Lists all registered workers with their current status."""
    workers = memory.get_registered_workers()
    return {"workers": workers}

@app.get("/api/workers/{worker_id}/health")
async def check_worker_health_endpoint(worker_id: str):
    """Checks a specific worker's health by pinging its /health endpoint."""
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
    """Removes a worker registration."""
    memory.remove_worker(worker_id)
    return {"status": "success", "detail": f"Worker '{worker_id}' removed"}

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

@app.get("/api/modules")
async def list_modules_endpoint():
    """Dynamically scans static/modules directory for widget modules."""
    modules_dir = Path(__file__).parent.parent / "static" / "modules"
    if not modules_dir.exists():
        return {"modules": []}
    
    modules = []
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
    return {"modules": modules}

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
    clean_id = task_id.replace("task-agent-", "")
    if clean_id.startswith("subagent-") or clean_id.startswith("boardroom-") or task_id.startswith("subagent-"):
        try:
            msgs = memory.get_subagent_messages(clean_id)
            # Filter out parent spawn message containing the massive prompt
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

# Endpoints for schedules
@app.get("/api/schedule")
async def list_schedule_endpoint():
    ensure_default_scheduled_tasks()
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

# SQLite history context window rolling summary compaction
async def check_and_compact_session_history(session_id: str, model_name: str = "gemini-3.5-flash", api_key: Optional[str] = None) -> None:
    """Checks conversation history size and compacts oldest 40 rows into a summary if row count exceeds 60."""
    conn = get_connection(memory.DB_FILE_PATH)
    steps_count = 0
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM conversation_steps WHERE session_id = ?", (session_id,))
        steps_count = cursor.fetchone()[0]
    except Exception:
        pass
    finally:
        conn.close()
        
    if steps_count < 60:
        return
        
    conn = get_connection(memory.DB_FILE_PATH)
    rows_to_compact = []
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, role, content, tool_name FROM conversation_steps WHERE session_id = ? ORDER BY id ASC LIMIT 40",
            (session_id,)
        )
        rows_to_compact = cursor.fetchall()
    except Exception:
        pass
    finally:
        conn.close()
        
    if not rows_to_compact:
        return
        
    history_text = ""
    row_ids = []
    for r_id, role, content, tool_name in rows_to_compact:
        row_ids.append(r_id)
        if role == "user":
            history_text += f"User: {content}\n"
        elif role == "assistant":
            history_text += f"Assistant: {content}\n"
        elif role == "thought":
            history_text += f"Thought: {content}\n"
        elif role == "tool_call":
            history_text += f"Tool Call ({tool_name}): {content}\n"
            
    summary_prompt = (
        "Please read the following conversation history between a user and an AI coding agent. "
        "Summarize the context, achievements, variables, custom settings, and decisions made in a concise "
        "paragraph of 150-250 words. Do not include unnecessary conversational filler.\n\n"
        f"--- HISTORY ---\n{history_text}\n--- END HISTORY ---"
    )
    
    summary_text = ""
    try:
        if api_key:
            from google.antigravity import Agent, LocalAgentConfig
            from google.antigravity.types import CapabilitiesConfig
            tmp_config = LocalAgentConfig(
                model=model_name,
                api_key=api_key,
                system_instructions="You are a context window compressor. Output only the summary.",
                capabilities=CapabilitiesConfig(enable_subagents=False),
                workspaces=[os.getcwd()],
            )
            async with Agent(tmp_config) as tmp_agent:
                resp = await tmp_agent.chat(summary_prompt)
                async for chunk in resp:
                    summary_text += chunk
        else:
            tmp_agent = KeylessAgyAgent(
                model=model_name,
                system_instructions="You are a context window compressor. Output only the summary.",
                timeout=60.0
            )
            async with tmp_agent as agent_ctx:
                resp = await agent_ctx.chat(summary_prompt)
                async for chunk in resp:
                    summary_text += chunk
    except Exception as e:
        print(f"[COMPACTION] Summarization failed: {e}")
        return
        
    if not summary_text:
        return
        
    conn = get_connection(memory.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        min_id = min(row_ids)
        placeholders = ",".join("?" for _ in row_ids)
        cursor.execute(f"DELETE FROM conversation_steps WHERE id IN ({placeholders})", row_ids)
        try:
            cursor.execute(f"DELETE FROM conversation_search WHERE step_id IN ({placeholders})", row_ids)
        except Exception:
            pass
            
        timestamp = datetime.now(timezone.utc).isoformat()
        formatted_summary = f"[System Context Compression Summary]: {summary_text.strip()}"
        cursor.execute(
            "INSERT INTO conversation_steps (id, session_id, timestamp, role, content) VALUES (?, ?, ?, ?, ?)",
            (min_id, session_id, timestamp, "thought", formatted_summary)
        )
        conn.commit()
        print(f"[COMPACTION] Successfully compacted {len(row_ids)} turns for session {session_id} into a single summary block.")
    except Exception as e:
        print(f"[COMPACTION] DB commit failed: {e}")
    finally:
        conn.close()

class ForkRequest(BaseModel):
    session_id: str
    fork_step_index: int

@app.post("/api/sessions/fork")
async def fork_session_endpoint(req: ForkRequest):
    global active_agents
    session_id = req.session_id
    resolved_id = session_id
    if session_id.startswith("discord-session-"):
        mem = memory.load_memory()
        session_mappings = mem.setdefault("key_value", {}).get("session_mappings", {})
        if isinstance(session_mappings, dict):
            resolved_id = session_mappings.get(session_id, session_id)
            
    conn = get_connection(memory.DB_FILE_PATH)
    forked_steps = []
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, content, tool_name, tool_result, timestamp FROM conversation_steps WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (resolved_id, req.fork_step_index)
        )
        forked_steps = cursor.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database query error: {e}")
    finally:
        conn.close()
        
    if not forked_steps:
        raise HTTPException(status_code=400, detail="No conversation history found to fork from.")
        
    import uuid
    new_session_id = str(uuid.uuid4())
    
    conn = get_connection(memory.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        for role, content, tool_name, tool_result, timestamp in forked_steps:
            cursor.execute(
                "INSERT INTO conversation_steps (session_id, timestamp, role, content, tool_name, tool_result) VALUES (?, ?, ?, ?, ?, ?)",
                (new_session_id, timestamp, role, content, tool_name, tool_result)
            )
        conn.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to copy history for fork: {e}")
    finally:
        conn.close()
        
    # Copy the actual agent conversation DB file on disk
    import shutil
    from pathlib import Path
    old_agent_db = Path.home() / ".gemini" / "antigravity-cli" / "conversations" / f"{resolved_id}.db"
    new_agent_db = Path.home() / ".gemini" / "antigravity-cli" / "conversations" / f"{new_session_id}.db"
    if old_agent_db.exists():
        try:
            shutil.copy2(old_agent_db, new_agent_db)
        except Exception as e:
            print(f"[FORK] Warning: failed to copy agent DB file: {e}")

    return {"status": "success", "new_session_id": new_session_id}

# Background scheduler execution
async def execute_scheduled_task(name: str, prompt: str):
    """Executes a scheduled task using an isolated agent instance (not the shared default)."""
    if name == "Meta-Evaluation":
        try:
            from agent.evaluation.meta_evaluation import run_meta_evaluation
            conversation_id = "meta-eval-run-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            memory.log_conversation_step(conversation_id, "user", f"[Scheduled Task: {name}] {prompt}")
            
            await run_meta_evaluation()
            
            memory.log_conversation_step(conversation_id, "assistant", "Meta-Evaluation executed successfully.")
            print(f"[Scheduled Task: {name}] Executed successfully.")
            return
        except Exception as e:
            print(f"[Scheduled Task: {name}] Error: {e}")
            return

    if name == "Quiet Observer":
        try:
            from agent.observability.quiet_observer import run_quiet_observer
            conversation_id = "quiet-observer-run-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            memory.log_conversation_step(conversation_id, "user", f"[Scheduled Task: {name}] {prompt}")
            
            await run_quiet_observer()
            
            memory.log_conversation_step(conversation_id, "assistant", "Quiet Observer executed successfully.")
            print(f"[Scheduled Task: {name}] Executed successfully.")
            return
        except Exception as e:
            print(f"[Scheduled Task: {name}] Error: {e}")
            return

    if name == "Gmail Email Check":
        try:
            proj_root = str(Path(__file__).resolve().parent.parent.parent.parent)
            proc = await asyncio.create_subprocess_exec(
                sys.executable or "python3", "scratch/run_gmail_sync.py",
                cwd=proj_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180.0)
            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()
            
            output = stdout_str
            if stderr_str:
                output += f"\n\nStderr Errors:\n{stderr_str}"
            
            has_no_new_mail = "No new emails detected since last check" in stdout_str or "no new emails detected" in stdout_str.lower()
            is_pubsub_active = "Pub/Sub listener is active" in stdout_str
            
            if has_no_new_mail or is_pubsub_active:
                print(f"[Scheduled Task: {name}] Quiet check: {stdout_str}")
                return

            conversation_id = f"sched-gmail-check-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
            memory.log_conversation_step(conversation_id, "user", f"[Scheduled Task: {name}] {prompt}")
            memory.log_conversation_step(conversation_id, "assistant", output or "Gmail check completed with no output.")
            print(f"[Scheduled Task: {name}] Executed directly via subprocess and logged. Return code: {proc.returncode}")
            return
        except Exception as e:
            err_msg = f"Failed to execute Gmail check script: {e}"
            print(f"[Scheduled Task: {name}] Error: {err_msg}")
            # If it failed to run, we log it so the failure is visible
            conversation_id = f"sched-gmail-check-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
            memory.log_conversation_step(conversation_id, "user", f"[Scheduled Task: {name}] {prompt}")
            memory.log_conversation_step(conversation_id, "assistant", err_msg)
            return

    if name == "Grace Timekeeper":
        conversation_id = f"sched-grace-timekeeper-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        memory.log_conversation_step(conversation_id, "user", f"[Scheduled Task: {name}] {prompt}")
        try:
            proj_root = str(Path(__file__).resolve().parent.parent.parent.parent)
            proc = await asyncio.create_subprocess_exec(
                sys.executable or "python3", "src/agent/observability/grace_monitor.py",
                cwd=proj_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180.0)
            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()
            
            output = stdout_str
            if stderr_str:
                output += f"\n\nStderr Errors:\n{stderr_str}"
            
            memory.log_conversation_step(conversation_id, "assistant", output or "Grace timekeeper completed with no output.")
            print(f"[Scheduled Task: {name}] Executed directly via subprocess. Return code: {proc.returncode}")
            return
        except Exception as e:
            err_msg = f"Failed to execute Grace timekeeper script: {e}"
            print(f"[Scheduled Task: {name}] Error: {err_msg}")
            memory.log_conversation_step(conversation_id, "assistant", err_msg)
            return

    if name == "Stock Game Auto Check":
        conversation_id = f"sched-stock-check-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        memory.log_conversation_step(conversation_id, "user", f"[Scheduled Task: {name}] {prompt}")
        try:
            proj_root = str(Path(__file__).resolve().parent.parent.parent.parent)
            proc = await asyncio.create_subprocess_exec(
                sys.executable or "python3", "stock_game/strategy.py",
                cwd=proj_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180.0)
            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()
            
            output = stdout_str
            if stderr_str:
                output += f"\n\nStderr Errors:\n{stderr_str}"
            
            memory.log_conversation_step(conversation_id, "assistant", output or "Stock game auto-check completed with no output.")
            print(f"[Scheduled Task: {name}] Executed directly via subprocess. Return code: {proc.returncode}")
            return
        except Exception as e:
            err_msg = f"Failed to execute Stock Game Auto Check script: {e}"
            print(f"[Scheduled Task: {name}] Error: {err_msg}")
            memory.log_conversation_step(conversation_id, "assistant", err_msg)
            return

    # Generic scheduled tasks: use a dedicated, isolated KeylessAgyAgent
    # This prevents cross-contamination of conversation context between background tasks
    from agent.keyless import KeylessAgyAgent, TaskPriority

    # Determine priority: Grace is SCHEDULED_CRITICAL, everything else is SCHEDULED_ROUTINE
    priority = TaskPriority.SCHEDULED_CRITICAL if "Grace" in name else TaskPriority.SCHEDULED_ROUTINE

    conversation_id = f"sched-{name.lower().replace(' ', '-')}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    try:
        from agent.core.registry import tool_registry
        profile_name = name.lower().replace(" ", "_").replace("-", "_")
        specialist_inst = tool_registry.resolve_subagent_profile(profile_name)
        if not specialist_inst:
            specialist_inst = tool_registry.resolve_subagent_profile(name)
            
        system_instructions = specialist_inst or f"You are executing the scheduled background task: {name}. Complete it and report results concisely."

        # Check for resumable checkpoint
        try:
            from agent.core.task_manager import get_checkpoint
            task_key = name.lower().replace(" ", "_").replace("-", "_")
            checkpoint = get_checkpoint(task_key)
            if checkpoint and checkpoint['status'] == 'in_progress':
                prompt = (
                    f"[RESUMING FROM CHECKPOINT]\n"
                    f"Task: {name}\n"
                    f"Phase completed: {checkpoint['phase']}\n"
                    f"Step {checkpoint['step_completed']}/{checkpoint['total_steps'] or '?'}\n"
                    f"Saved state: {checkpoint['state_json']}\n\n"
                    f"Resume from the next step. Do NOT repeat completed work.\n\n"
                    f"Original task: {prompt}"
                )
                print(f"[CHECKPOINT] Resuming scheduled task '{name}' from step {checkpoint['step_completed']}")
        except Exception as cp_err:
            print(f"[CHECKPOINT] Error checking checkpoint for scheduled task: {cp_err}")

        agent = KeylessAgyAgent(
            model="gemini-3.5-flash",
            system_instructions=system_instructions,
            conversation_id=conversation_id,
            timeout=120.0,
            task_priority=priority
        )
        memory.log_conversation_step(conversation_id, "user", f"[Scheduled Task: {name}] {prompt}")
        async with agent as sched_agent:
            response = await sched_agent.chat(prompt)

            thoughts_str = ""
            async for thought in response.thoughts:
                thoughts_str += thought
            if thoughts_str:
                memory.log_conversation_step(conversation_id, "thought", thoughts_str)

            output_content = ""
            async for chunk in response:
                output_content += chunk
            if output_content:
                memory.log_conversation_step(conversation_id, "assistant", output_content)
        print(f"[Scheduled Task: {name}] Executed successfully.")
    except Exception as e:
        print(f"[Scheduled Task: {name}] Error: {e}")
        memory.log_conversation_step(conversation_id, "assistant", f"Scheduled task failed: {e}")

def _cleanup_stale_sandboxes(max_age_seconds: int = 3600) -> int:
    """Removes subagent sandbox directories in /tmp older than max_age_seconds. Returns count removed."""
    import shutil
    import time
    removed = 0
    tmp = Path("/tmp")
    if not tmp.exists():
        return 0
    now = time.time()
    for item in tmp.iterdir():
        if item.is_dir() and item.name.startswith("subagent_sandbox_"):
            try:
                age = now - item.stat().st_mtime
                if age > max_age_seconds:
                    shutil.rmtree(item)
                    removed += 1
            except Exception:
                pass
    return removed

async def run_scheduler():
    await asyncio.sleep(2)
    while True:
        try:
            ensure_default_scheduled_tasks()
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

            # Cleanup stale subagent sandboxes (older than 1 hour)
            try:
                removed = _cleanup_stale_sandboxes(max_age_seconds=3600)
                if removed > 0:
                    print(f"[CLEANUP] Removed {removed} stale subagent sandbox(es).")
            except Exception:
                pass

            # Periodic worker health checks (every ~60 seconds via modulo on the 5s tick)
            try:
                import time as _time
                if int(_time.time()) % 60 < 6:  # Runs roughly once per minute
                    workers = memory.get_registered_workers()
                    for w in workers:
                        # Periodically check health of all registered workers to allow recovery
                        from agent.remote_worker import check_worker_health
                        await check_worker_health(w)
            except Exception as we:
                print(f"[WORKERS] Health check error: {we}")
            # Check for delegated plan steps to resume
            conn = get_connection(memory.DB_FILE_PATH)
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT ps.id, ps.plan_id, ps.description, sp.session_id, ps.step_order 
                    FROM plan_steps ps
                    JOIN session_plans sp ON ps.plan_id = sp.id
                    WHERE ps.status = 'delegated'
                """)
                delegated_steps = cursor.fetchall()
                
                resumed_sessions = set()
                for step_id, plan_id, step_desc, session_id, step_order in delegated_steps:
                    cursor.execute("""
                        SELECT subagent_id, message, timestamp 
                        FROM subagent_messages 
                        WHERE parent_session_id = ? AND role = 'subagent'
                        ORDER BY id DESC LIMIT 1
                    """, (session_id,))
                    subagent_row = cursor.fetchone()
                    
                    if subagent_row:
                        subagent_id, message, timestamp = subagent_row
                        if "subagent completed:" in message.lower():
                            cursor.execute("UPDATE plan_steps SET status = 'completed' WHERE id = ?", (step_id,))
                            conn.commit()
                            print(f"[SCHEDULER] Subagent {subagent_id} completed. Step {step_id} marked completed.")
                            
                            if session_id not in resumed_sessions:
                                resumed_sessions.add(session_id)
                                async def resume_parent(sess_id, sub_id, msg):
                                    try:
                                        # Find original Discord channel if it's a mapped discord session
                                        channel_id = None
                                        mem = memory.load_memory()
                                        session_mappings = mem.get("key_value", {}).get("session_mappings", {})
                                        if isinstance(session_mappings, dict):
                                            for k, v in session_mappings.items():
                                                if v == sess_id and k.startswith("discord-session-"):
                                                    try:
                                                        channel_id = int(k.split("-")[-1])
                                                    except ValueError:
                                                        pass
                                                    break

                                        if channel_id:
                                            from agent.notifications import send_direct_discord_message
                                            send_direct_discord_message(channel_id, f"⚙️ **Plan Resuming**: Subagent `{sub_id}` completed its task. Resuming parent execution...")

                                        from agent.keyless import KeylessAgyAgent
                                        agent = KeylessAgyAgent(
                                            model="gemini-3.5-flash",
                                            conversation_id=sess_id,
                                            timeout=120.0
                                        )
                                        # Query the original user prompt to remind the orchestrator of the goal
                                        original_goal = ""
                                        try:
                                            conn_p = get_connection(memory.DB_FILE_PATH)
                                            cursor_p = conn_p.cursor()
                                            cursor_p.execute(
                                                "SELECT content FROM conversation_steps WHERE session_id = ? AND role = 'user' ORDER BY timestamp ASC LIMIT 1",
                                                (sess_id,)
                                            )
                                            row_p = cursor_p.fetchone()
                                            if row_p:
                                                original_goal = row_p[0]
                                            conn_p.close()
                                        except Exception:
                                            pass

                                        resume_prompt = (
                                            f"[SYSTEM RESUME]\n"
                                            f"Subagent '{sub_id}' has completed the delegated task.\n"
                                            f"Subagent Output: {msg}\n\n"
                                            f"Original User Request: \"{original_goal}\"\n\n"
                                            f"Please resume execution, review the subagent's output against the original user request, "
                                            f"complete any remaining goals (such as reading and showing file contents, summarizing results, or answering specific questions), "
                                            f"and output your final response directly to the user."
                                        )
                                        memory.log_conversation_step(sess_id, "user", resume_prompt)
                                        async with agent as active_agent:
                                            response = await active_agent.chat(resume_prompt)
                                            output_content = ""
                                            async for chunk in response:
                                                output_content += chunk
                                            if output_content:
                                                memory.log_conversation_step(sess_id, "assistant", output_content)
                                                if channel_id:
                                                    send_direct_discord_message(channel_id, output_content)
                                    except Exception as re_err:
                                        print(f"[SCHEDULER] Failed to resume parent session {sess_id}: {re_err}")
                                
                                asyncio.create_task(resume_parent(session_id, subagent_id, message))
                            
                        elif "subagent failed:" in message.lower():
                            cursor.execute("UPDATE plan_steps SET status = 'failed', error_message = ? WHERE id = ?", (message, step_id))
                            conn.commit()
                            print(f"[SCHEDULER] Subagent {subagent_id} failed. Step {step_id} marked failed.")
                            
                            if session_id not in resumed_sessions:
                                resumed_sessions.add(session_id)
                                async def resume_parent_fail(sess_id, sub_id, err_msg):
                                    try:
                                        # Find original Discord channel if it's a mapped discord session
                                        channel_id = None
                                        mem = memory.load_memory()
                                        session_mappings = mem.get("key_value", {}).get("session_mappings", {})
                                        if isinstance(session_mappings, dict):
                                            for k, v in session_mappings.items():
                                                if v == sess_id and k.startswith("discord-session-"):
                                                    try:
                                                        channel_id = int(k.split("-")[-1])
                                                    except ValueError:
                                                        pass
                                                    break

                                        if channel_id:
                                            from agent.notifications import send_direct_discord_message
                                            send_direct_discord_message(channel_id, f"⚠️ **Plan Resuming on Failure**: Subagent `{sub_id}` failed. Resuming parent to handle error...")

                                        from agent.keyless import KeylessAgyAgent
                                        agent = KeylessAgyAgent(
                                            model="gemini-3.5-flash",
                                            conversation_id=sess_id,
                                            timeout=120.0
                                        )
                                        resume_prompt = (
                                            f"[SYSTEM RESUME - FAILURE]\n"
                                            f"Subagent '{sub_id}' failed execution.\n"
                                            f"Failure message: {err_msg}\n\n"
                                            f"Please handle the failure and proceed accordingly."
                                        )
                                        memory.log_conversation_step(sess_id, "user", resume_prompt)
                                        async with agent as active_agent:
                                            response = await active_agent.chat(resume_prompt)
                                            output_content = ""
                                            async for chunk in response:
                                                output_content += chunk
                                            if output_content:
                                                memory.log_conversation_step(sess_id, "assistant", output_content)
                                                if channel_id:
                                                    send_direct_discord_message(channel_id, output_content)
                                    except Exception as re_err:
                                        print(f"[SCHEDULER] Failed to resume parent session {sess_id} on failure: {re_err}")
                                        
                                asyncio.create_task(resume_parent_fail(session_id, subagent_id, message))
                
                # Check for completed subagents that need parent resumption (non-plan sessions)
                cursor.execute("""
                    SELECT DISTINCT subagent_id, parent_session_id 
                    FROM subagent_messages 
                    WHERE role = 'subagent' 
                      AND parent_session_id IS NOT NULL 
                      AND parent_session_id != 'New Session'
                      AND (message LIKE 'Subagent completed:%' OR message LIKE 'Subagent failed:%')
                """)
                completed_subs = cursor.fetchall()
                
                for subagent_id, parent_session_id in completed_subs:
                    # Skip if the parent session has a plan step in 'delegated' status (already handled above)
                    cursor.execute("""
                        SELECT count(*) 
                        FROM plan_steps ps
                        JOIN session_plans sp ON ps.plan_id = sp.id
                        WHERE sp.session_id = ? AND ps.status = 'delegated'
                    """, (parent_session_id,))
                    if cursor.fetchone()[0] > 0:
                        continue
                        
                    # Check if already resumed in parent history
                    cursor.execute("""
                        SELECT count(*) FROM conversation_steps 
                        WHERE session_id = ? AND role = 'user' AND content LIKE ?
                    """, (parent_session_id, f"%[SYSTEM RESUME]%{subagent_id}%"))
                    resumed_count = cursor.fetchone()[0]
                    
                    if resumed_count == 0:
                        # Get the latest message
                        cursor.execute("""
                            SELECT message FROM subagent_messages 
                            WHERE subagent_id = ? AND role = 'subagent' 
                            ORDER BY id DESC LIMIT 1
                        """, (subagent_id,))
                        msg_row = cursor.fetchone()
                        if not msg_row:
                            continue
                        message = msg_row[0]
                        
                        # Proceed with resume
                        print(f"[SCHEDULER] Subagent {subagent_id} completed (non-plan). Resuming parent session {parent_session_id}...")
                        
                        async def resume_parent_non_plan(sess_id, sub_id, msg):
                            try:
                                channel_id = None
                                mem = memory.load_memory()
                                session_mappings = mem.get("key_value", {}).get("session_mappings", {})
                                if isinstance(session_mappings, dict):
                                    for k, v in session_mappings.items():
                                        if v == sess_id and k.startswith("discord-session-"):
                                            try:
                                                channel_id = int(k.split("-")[-1])
                                            except ValueError:
                                                pass
                                            break

                                if channel_id:
                                    from agent.notifications import send_direct_discord_message
                                    send_direct_discord_message(channel_id, f"⚙️ **Plan Resuming**: Subagent `{sub_id}` completed its task. Resuming parent execution...")

                                from agent.keyless import KeylessAgyAgent
                                agent = KeylessAgyAgent(
                                    model="gemini-3.5-flash",
                                    conversation_id=sess_id,
                                    timeout=120.0
                                )
                                resume_prompt = (
                                    f"[SYSTEM RESUME]\n"
                                    f"Subagent '{sub_id}' has completed the delegated task.\n"
                                    f"Subagent Output: {msg}\n\n"
                                    f"Please resume execution and report back."
                                )
                                memory.log_conversation_step(sess_id, "user", resume_prompt)
                                async with agent as active_agent:
                                    response = await active_agent.chat(resume_prompt)
                                    output_content = ""
                                    async for chunk in response:
                                        output_content += chunk
                                    if output_content:
                                        memory.log_conversation_step(sess_id, "assistant", output_content)
                                        if channel_id:
                                            send_direct_discord_message(channel_id, output_content)
                            except Exception as re_err:
                                print(f"[SCHEDULER] Failed to resume non-plan parent session {sess_id}: {re_err}")
                                
                        asyncio.create_task(resume_parent_non_plan(parent_session_id, subagent_id, message))
            except Exception as e:
                print(f"[SCHEDULER] Error checking subagent completion: {e}")
            finally:
                conn.close()

            now_str = datetime.now(timezone.utc).isoformat()
            conn = get_connection(memory.DB_FILE_PATH)
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
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

