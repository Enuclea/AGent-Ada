import os
import json
import sqlite3
import mimetypes
import asyncio
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import HTTPException, Depends, Request, Response
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
from pydantic import BaseModel, Field

from agent.api.router import app
from agent import memory, tools
from agent.storage.db import get_connection
from agent.core.orchestrator import orchestration_service

# Import active_subagents for stop route logic
from agent.core.subagent_manager import active_subagents
from agent.api.subagents import SpawnSubagentRequest, spawn_subagent_endpoint

def _is_plain_chat(text: str) -> bool:
    """Server-side intent inference: returns True if the prompt looks like casual chat.
    
    Prevents casual messages from entering the full coordinator/delegation pipeline.
    Conservative: short messages default to chat, task-verb messages default to work.
    """
    text_lower = text.strip().lower()
    if len(text_lower) < 30:
        command_prefixes = [
            "run ", "check ", "deploy ", "fix ", "update ", "restart ",
            "show ", "list ", "create ", "delete ", "add ", "remove ",
            "investigate ", "diagnose ", "debug ", "test ", "build ",
        ]
        if any(text_lower.startswith(p) for p in command_prefixes):
            return False
        return True
    task_signals = [
        "implement", "refactor", "deploy", "debug", "fix the", "create a",
        "write a", "add a", "remove the", "update the", "change the",
        "run the", "restart", "check the logs", "investigate", "diagnose",
        "commit", "push", "merge", "review the", "test the", "build",
        "configure", "install", "set up", "look into", "what's the status",
        "pull request", "pr ", "branch", "git ",
    ]
    if any(signal in text_lower for signal in task_signals):
        return False
    return True

class ChatRequest(BaseModel):
    prompt: str = Field(..., max_length=10000)
    session_id: Optional[str] = Field(None, max_length=128)
    model: Optional[str] = Field(None, max_length=128)
    system_instructions: Optional[str] = Field(None, max_length=32768)
    agent_profile: Optional[str] = Field(None, max_length=128)
    disable_tools: Optional[bool] = False
    roleplay: Optional[bool] = False
    general_chat: Optional[bool] = False

async def get_or_create_agent(
    model_name: Optional[str] = None,
    session_id: Optional[str] = None,
    system_instructions: Optional[str] = None,
    disable_tools: bool = False,
    roleplay: bool = False,
    general_chat: bool = False,
    prompt: Optional[str] = None,
    agent_profile: Optional[str] = None
):
    is_discord = session_id is not None and (session_id.startswith("discord-session-") or session_id.startswith("discord-roleplay-"))
    if not is_discord:
        _ = tools.backup_discord_channel

    # Resolve specialist profile instructions if provided
    if agent_profile and not system_instructions:
        from agent.core.registry import tool_registry
        specialist_inst = tool_registry.resolve_subagent_profile(agent_profile)
        if specialist_inst:
            system_instructions = specialist_inst

    auto_approve = not is_discord

    return await orchestration_service.get_or_create_agent(
        model=model_name,
        session_id=session_id,
        custom_instructions=system_instructions,
        disable_tools=disable_tools,
        roleplay=roleplay,
        general_chat=general_chat,
        prompt=prompt,
        auto_approve=auto_approve,
        agent_profile=agent_profile
    )

@app.post("/api/chat")
async def chat_endpoint(request: Request):
    body_bytes = await request.body()
    try:
        payload = json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    if "messages" in payload:
        if os.environ.get("ADA_ENABLE_OLLAMA_ENDPOINT", "0").strip().lower() not in ("1", "true", "yes"):
            raise HTTPException(
                status_code=403,
                detail="Ollama-compatible endpoint is disabled. Set ADA_ENABLE_OLLAMA_ENDPOINT=1 in .env to enable."
            )
        from agent.api.ollama_clone import ollama_chat_endpoint, OllamaChatRequest
        try:
            req = OllamaChatRequest(**payload)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid Ollama chat request: {e}")
        return await ollama_chat_endpoint(req)
        
    try:
        req = ChatRequest(**payload)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid ChatRequest: {e}")
    global active_agents
    active_agents = orchestration_service.active_agents
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
                
        cancelled_subs = []
        for subagent_id, sub_info in list(active_subagents.items()):
            if sub_info.get("parent_session_id") == resolved_id:
                if sub_info.get("task"):
                    sub_info["task"].cancel()
                resp = sub_info.get("response")
                if resp and resp.proc:
                    try:
                        resp.proc.kill()
                    except Exception:
                        pass
                active_subagents.pop(subagent_id, None)
                cancelled_subs.append(subagent_id)
                
        conn = get_connection(memory.DB_FILE_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE plan_steps 
                SET status = 'failed', error_message = 'Terminated by user stop command.' 
                WHERE plan_id IN (SELECT id FROM session_plans WHERE session_id = ?)
                  AND status IN ('pending', 'delegated', 'running')
            """, (resolved_id,))
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()
            
        async def stop_generator():
            yield f"data: {json.dumps({'type': 'chunk', 'content': '🛑 Session termination sequence complete. Subagents killed.'})}\n\n"
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

    # Mode 3: Roleplay is pure entertainment — ZERO tools
    effective_disable_tools = req.disable_tools
    if req.roleplay:
        effective_disable_tools = True

    # Server-side intent inference: auto-detect plain chat if not explicitly set
    # This prevents casual messages from going through the full coordinator/delegation pipeline
    effective_general_chat = getattr(req, 'general_chat', False)
    if not effective_general_chat and not req.roleplay and req.prompt:
        effective_general_chat = _is_plain_chat(req.prompt)

    try:
        from agent.web import get_or_create_agent as get_agent_fn
        agent = await get_agent_fn(
            req.model,
            req.session_id,
            resolved_system_instructions,
            effective_disable_tools,
            req.roleplay,
            general_chat=effective_general_chat,
            prompt=req.prompt,
            agent_profile=req.agent_profile
        )
    except Exception as e:
        lookup_id = req.session_id or "default"
        orchestration_service.active_agents.pop(lookup_id, None)
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
        from agent.web import get_or_create_agent as get_agent_fn
        lookup_id = req.session_id or "default"
        queue = asyncio.Queue()

        async def run_agent():
            from agent.memory import active_session_id_var
            from agent.api.router import get_session_lock
            token = active_session_id_var.set(agent.conversation_id)
            lock = get_session_lock(lookup_id)
            
            # Output Gate: track whether any text content was yielded to the client
            text_chunks_emitted = False
            thoughts_emitted = False
            
            async def stream_agent_response(active_agent, prompt_to_send):
                nonlocal text_chunks_emitted, thoughts_emitted
                # Reset context state to False to prevent session leakage
                from agent.tools import yield_requested
                yield_requested.set(False)

                response = await active_agent.chat(prompt_to_send)
                if hasattr(active_agent, "grok_auth_alert") and active_agent.grok_auth_alert:
                    await queue.put({"type": "grok_auth_alert", "content": active_agent.grok_auth_alert})
                    active_agent.grok_auth_alert = None
                await queue.put({"type": "session_id", "content": active_agent.conversation_id})
                
                # Stream thoughts
                thoughts_str = ""
                async for thought in response.thoughts:
                    thoughts_str += thought
                    if thought:
                        thoughts_emitted = True
                        await queue.put({"type": "thought", "content": thought})
                    if yield_requested.get():
                        break
                    
                if thoughts_str:
                    memory.log_conversation_step(active_agent.conversation_id, "thought", thoughts_str)
                    
                if active_agent.conversation_id and req.session_id:
                    from agent.web import update_session_mapping
                    update_session_mapping(req.session_id, active_agent.conversation_id)
                    await queue.put({"type": "session_id", "content": active_agent.conversation_id})

                # If yield was requested (e.g. subagent spawned), give the subprocess
                # a short grace period to finish its output, then terminate it.
                if yield_requested.get() and hasattr(response, 'proc') and response.proc and response.proc.returncode is None:
                    print(f"[YIELD] yield_requested detected for session {active_agent.conversation_id}. Draining subprocess with 5s grace period...")
                    grace_output = ""
                    try:
                        grace_deadline = asyncio.get_event_loop().time() + 5.0
                        while asyncio.get_event_loop().time() < grace_deadline:
                            try:
                                chunk_bytes = await asyncio.wait_for(response.proc.stdout.read(4096), timeout=1.0)
                                if not chunk_bytes:
                                    break
                                decoded = chunk_bytes.decode("utf-8", errors="replace")
                                grace_output += decoded
                            except asyncio.TimeoutError:
                                if response.proc.returncode is not None:
                                    break
                                continue
                    except Exception:
                        pass
                    # Kill the subprocess if it's still alive
                    if response.proc.returncode is None:
                        try:
                            response.proc.kill()
                            await response.proc.wait()
                            print(f"[YIELD] Subprocess terminated after grace period for session {active_agent.conversation_id}")
                        except Exception:
                            pass
                    # Emit any grace period output as final text
                    if grace_output.strip():
                        from agent.security.pipeline import sanitize_output
                        sanitized_grace = sanitize_output(grace_output)
                        if sanitized_grace.strip():
                            text_chunks_emitted = True
                            await queue.put({"type": "chunk", "content": sanitized_grace})
                            memory.log_conversation_step(active_agent.conversation_id, "assistant", sanitized_grace)
                    return
                    
                # Stream response chunks ONLY if a yield has not been requested
                output_content = ""
                if not yield_requested.get():
                    async for chunk in response:
                        output_content += chunk
                        if chunk:
                            text_chunks_emitted = True
                            await queue.put({"type": "chunk", "content": chunk})
                        if yield_requested.get():
                            break
                    
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
                memory.log_token_usage(active_agent.conversation_id, active_agent.model or "gemini-3.6-flash", input_tokens, output_tokens, cost)

            try:
                await lock.acquire(priority)
                
                # Direct Roleplay / General Chat Bypass: Skip planning, decomposition, and sequential driving steps
                if req.roleplay or effective_general_chat or effective_disable_tools:
                    await stream_agent_response(agent, req.prompt)
                    return

                # 1. Decompose plan steps if enabled and prompt is complex enough
                enable_planning = os.environ.get("AGENT_ENABLE_PLAN_DECOMPOSITION", "true").lower() == "true"
                existing_plan = memory.get_session_plan(lookup_id)
                if existing_plan:
                    plan_status = existing_plan.get("status", "")
                    steps = existing_plan.get("steps", [])
                    all_steps_done = steps and all(s.get("status") in ("completed", "failed") for s in steps)
                    if plan_status == "completed" or all_steps_done:
                        existing_plan = None
                # Guard: Never re-decompose system-injected resume/driver prompts into new plans.
                is_system_prompt = req.prompt.strip().startswith("[SYSTEM")
                plan_min_length = int(os.environ.get("AGENT_PLAN_MIN_LENGTH", "40"))
                if enable_planning and not existing_plan and not is_system_prompt and len(req.prompt.strip()) > plan_min_length:
                    import uuid
                    plan_id = str(uuid.uuid4())
                    
                    # Fast-path: if the user explicitly names a specialist, skip LLM decomposition
                    # and create a single delegation step. This prevents subagent explosion.
                    import re
                    prompt_lower = req.prompt.lower()
                    specialist_match = None
                    for name in ["lacie", "val", "kira", "grace"]:
                        if re.search(r"\b" + re.escape(name) + r"\b", prompt_lower):
                            specialist_match = name
                            break
                    
                    if specialist_match:
                        # Single-step delegation — give the whole task to the named specialist
                        title = f"Delegate to {specialist_match.title()}: {req.prompt[:50]}..."
                        memory.add_session_plan(plan_id, lookup_id, title, "running", req.prompt, "[]", "[]")
                        step_id = f"step-{plan_id}-0"
                        memory.add_plan_step(
                            step_id=step_id,
                            plan_id=plan_id,
                            step_order=1,
                            description=req.prompt,
                            status="pending",
                            assigned_tool="spawn_subagent",
                            assigned_args=""
                        )
                        await queue.put({"type": "thought", "content": f"🚀 Delegating to {specialist_match.title()}...\n"})
                    else:
                        # Full LLM-based plan decomposition for complex tasks
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
                                model="gemini-3.6-flash",
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
                                    
                                    memory.add_session_plan(plan_id, lookup_id, title, "running", goal, ac, ng)
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
                primary_model = req.model or "gemini-3.6-flash"
                
                # Check Gemini quota
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

                # Run sequential driving steps
                current_plan = memory.get_session_plan(lookup_id)
                if current_plan and "steps" in current_plan:
                    steps_to_run = [s for s in current_plan["steps"] if s["status"] != "completed"]
                    if any(s["status"] in ("running", "delegated") for s in steps_to_run):
                        steps_to_run = []
                else:
                    steps_to_run = []

                if steps_to_run:
                    total_steps = len(current_plan["steps"])
                    
                    # Check if the task involves delegation
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
                        
                        delegation_session_id = agent.conversation_id
                        
                        async def background_delegation():
                            await asyncio.sleep(0.5)  # Wait for client lock release
                            from agent.memory import active_session_id_var
                            lock = get_session_lock(lookup_id)
                            token = active_session_id_var.set(delegation_session_id)
                            try:
                                await lock.acquire(priority=0)
                                import uuid
                                
                                for step in delegation_steps:
                                    step_desc = step.get("description", "")
                                    step_id = step["id"]
                                    
                                    # Determine agent profile
                                    profile = "ops_runner"
                                    desc_lower = step_desc.lower()
                                    if "lacie" in desc_lower:
                                        profile = "lacie"
                                    elif "val" in desc_lower or "qa" in desc_lower or "verif" in desc_lower:
                                        profile = "qa_specialist"
                                    elif "grace" in desc_lower:
                                        profile = "grace_timekeeper"
                                    elif "kira" in desc_lower:
                                        profile = "ops_runner"
                                    
                                    subagent_id = f"subagent-{profile}-{uuid.uuid4().hex[:8]}"
                                    prompt = f"{step_desc}\n\nOriginal user request: {req.prompt}"
                                    
                                    memory.log_subagent_message(subagent_id, "parent",
                                        f"Spawning tooled subagent ({profile}) with prompt: {prompt[:200]}")
                                    
                                    spawn_req = SpawnSubagentRequest(
                                        subagent_id=subagent_id,
                                        prompt=prompt,
                                        agent_profile=profile,
                                        parent_session_id=delegation_session_id or lookup_id,
                                        target_files=[],
                                        stub_files=[]
                                    )
                                    try:
                                        await spawn_subagent_endpoint(spawn_req)
                                        memory.log_subagent_message(subagent_id, "subagent",
                                            f"[SPAWNED] Subagent successfully spawned in background.")
                                        memory.update_plan_step_status(step_id, "delegated")
                                        print(f"[BG DISPATCH] Spawned {profile} ({subagent_id}) for step: {step_desc[:80]}")
                                    except Exception as spawn_err:
                                        print(f"[BG DISPATCH] Failed to spawn {profile}: {spawn_err}")
                                        memory.update_plan_step_status(step_id, "failed", error_message=str(spawn_err))
                                
                                summary = f"Dispatched {len(delegation_steps)} subagent(s) directly."
                                memory.log_conversation_step(delegation_session_id, "assistant", summary)
                                print(f"[BG DISPATCH] {summary}")
                            except Exception as bg_err:
                                print(f"[BG DISPATCH] Delegation failed: {bg_err}")
                                for s in delegation_steps:
                                    memory.update_plan_step_status(s["id"], "failed", error_message=str(bg_err))
                            finally:
                                active_session_id_var.reset(token)
                                lock.release()
                        
                        asyncio.create_task(background_delegation())
                        
                        await queue.put({"type": "session_id", "content": agent.conversation_id})
                        task_list = "\n".join([f"- {s['description']}" for s in delegation_steps])
                        await queue.put({"type": "chunk", "content": (
                             f"🚀 **Delegating {len(delegation_steps)} task(s) to background agents:**\n\n"
                             f"{task_list}\n\n"
                             f"Track progress in the **Activity Feed** →\n"
                             f"Subagents will appear as they spawn and complete."
                        )})
                        text_chunks_emitted = True
                        
                        for s in non_delegation_steps:
                            memory.update_plan_step_status(s["id"], "delegated")
                    else:
                        # No delegation steps — run all steps sequentially
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
                                active_agent = await get_agent_fn(
                                    primary_model,
                                    req.session_id,
                                    resolved_system_instructions,
                                    effective_disable_tools,
                                    req.roleplay,
                                    general_chat=False,
                                    prompt=driver_prompt,
                                    agent_profile=req.agent_profile
                                )
                                await stream_agent_response(active_agent, driver_prompt)
                                
                                # Check if a subagent was spawned
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
                                
                                fallback_model = "Claude Sonnet 4.6 (Thinking)" if is_gemini else "gemini-3.6-flash"
                                await queue.put({"type": "thought", "content": f"\n⚠️ [System: Step {step_order} failed. Retrying step with fallback model...]\n"})
                                
                                fallback_prompt = (
                                    f"[SYSTEM DRIVER - FALLBACK]\n"
                                    f"The previous attempt to execute Step {step_order} failed with error: {step_err}.\n"
                                    f"Original step task: \"{step_desc}\".\n"
                                    f"Please complete this step successfully now."
                                )
                                try:
                                    active_agents.pop(lookup_id, None)
                                    fallback_agent = await get_agent_fn(
                                        fallback_model,
                                        req.session_id,
                                        resolved_system_instructions,
                                        effective_disable_tools,
                                        req.roleplay,
                                        general_chat=False,
                                        prompt=fallback_prompt,
                                        agent_profile=req.agent_profile
                                    )
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
                        active_agent = await get_agent_fn(
                            primary_model,
                            req.session_id,
                            resolved_system_instructions,
                            effective_disable_tools,
                            req.roleplay,
                            general_chat=effective_general_chat,
                            prompt=req.prompt,
                            agent_profile=req.agent_profile
                        )
                        await stream_agent_response(active_agent, req.prompt)
                    except Exception as first_error:
                        print(f"[STUCK PREVENTION] Primary model ({primary_model}) failed: {first_error}. Triggering fallback double check.")
                        orchestration_service.active_agents.pop(lookup_id, None)
                        
                        await queue.put({"type": "thought", "content": "\n⚠️ [System: Model got stuck/errored. Retrying with fallback model...]\n"})
                        
                        fallback_model = "Claude Sonnet 4.6 (Thinking)" if is_gemini else "gemini-3.6-flash"
                        fallback_prompt = f"The previous model run got stuck/encountered an error. Please analyze and solve it.\n\nOriginal prompt: {req.prompt}"
                        
                        try:
                            fallback_agent = await get_agent_fn(
                                fallback_model,
                                req.session_id,
                                resolved_system_instructions,
                                effective_disable_tools,
                                req.roleplay,
                                general_chat=effective_general_chat,
                                prompt=fallback_prompt,
                                agent_profile=req.agent_profile
                            )
                            await stream_agent_response(fallback_agent, fallback_prompt)
                        except Exception as second_error:
                            print(f"[STUCK PREVENTION] Fallback model ({fallback_model}) also failed: {second_error}")
                            raise second_error

                # Output Gate
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
                if 'step_id' in locals() and step_id:
                    memory.update_plan_step_status(step_id, "failed", error_message=str(e))
                await queue.put(e)
            finally:
                active_session_id_var.reset(token)
                lock.release()

        task = asyncio.create_task(run_agent())
        try:
            while True:
                if task.done() and queue.empty():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=2.0)
                    if item == "DONE":
                        break
                    elif isinstance(item, Exception):
                        yield f"data: {json.dumps({'type': 'chunk', 'content': f'🌸 Session encountered execution error: {item}'})}\n\n"
                        break
                    else:
                        yield f"data: {json.dumps(item)}\n\n"
                except asyncio.TimeoutError:
                    if task.done() and queue.empty():
                        break
                    yield f"data: {json.dumps({'type': 'ping', 'content': 'ping'})}\n\n"
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


@app.get("/api/sessions")
async def sessions_endpoint():
    save_dir = Path.home() / ".agent" / "sessions"
    keyless_dir = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
    entries = []
    seen = set()
    
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
    from agent.web import get_or_create_agent as get_agent_fn
    await get_agent_fn(session_id=session_id)
    return {"status": "success", "session_id": session_id}

@app.get("/api/gemini/file")
async def get_gemini_file(path: str):
    if len(path) > 512:
        raise HTTPException(status_code=400, detail="Path too long")
        
    from pathlib import PurePosixPath
    pure = PurePosixPath(path)
    if ".." in pure.parts:
        raise HTTPException(status_code=400, detail="Directory traversal attempt detected")
        
    resolved_path = Path(os.path.realpath(path))
    
    allowed_roots = [
        Path(os.path.realpath("/app/.gemini")),
        Path(os.path.realpath("/app/scratch")),
        Path(os.path.realpath("/data"))
    ]
    
    is_allowed = False
    for root in allowed_roots:
        try:
            # Secure containment validation using os.path.commonpath
            if os.path.commonpath([str(root), str(resolved_path)]) == str(root):
                is_allowed = True
                break
        except ValueError:
            pass
            
    if not is_allowed:
        raise HTTPException(status_code=403, detail="Access denied")
        
    if not resolved_path.exists() or not resolved_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
        
    content_type, _ = mimetypes.guess_type(str(resolved_path))
    if not content_type:
        content_type = "application/octet-stream"
        
    return FileResponse(str(resolved_path), media_type=content_type)

def _get_zerotier_ips() -> list[str]:
    import socket
    import fcntl
    import struct
    ips = []
    try:
        for _, name in socket.if_nameindex():
            if name.startswith("zt"):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    ip = socket.inet_ntoa(fcntl.ioctl(
                        s.fileno(),
                        0x8915,  # SIOCGIFADDR
                        struct.pack('256s', name[:15].encode('utf-8'))
                    )[20:24])
                    ips.append(ip)
                except Exception:
                    pass
                finally:
                    s.close()
    except Exception:
        pass
    return ips

@app.get("/api/status")
async def status_endpoint(session_id: Optional[str] = None):
    from agent import __version__
    from agent.api.router import get_session_lock
    lookup_id = session_id or "default"
    from agent.web import get_or_create_agent as get_agent_fn
    try:
        agent = await get_agent_fn(session_id=session_id)
    except Exception as e:
        orchestration_service.active_agents.pop(lookup_id, None)
        raise HTTPException(status_code=500, detail=f"Agent connection error: {e}")
    
    from agent.core.registry import tool_registry
    skills = tool_registry.discover_skills()
    skills_list = [{"name": s.name, "description": s.description} for s in skills]
                        
    session_data = orchestration_service.active_agents.get(lookup_id, {})
    return {
        "status": "busy" if get_session_lock(lookup_id)._locked else "ready",
        "version": __version__,
        "model": session_data.get("model", "gemini-3.6-flash"),
        "workspace": os.getcwd(),
        "session_id": agent.conversation_id,
        "skills": skills_list,
        "network": {
            "zerotier_ips": _get_zerotier_ips()
        }
    }

@app.get("/api/history")
async def history_endpoint(session_id: Optional[str] = None):
    resolved_id = None
    if session_id:
        if session_id.startswith("discord-session-"):
            mem = memory.load_memory()
            session_mappings = mem.get("key_value", {}).get("session_mappings", {})
            if isinstance(session_mappings, dict):
                resolved_id = session_mappings.get(session_id)
        else:
            resolved_id = session_id
    else:
        default_session = orchestration_service.active_agents.get("default")
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

@app.get("/api/sessions/{session_id}/plan")
async def get_session_plan_endpoint(session_id: str):
    plan = memory.get_session_plan(session_id)
    return {"plan": plan}

@app.get("/api/sessions/{session_id}/telemetry")
async def get_session_telemetry_endpoint(session_id: str):
    telemetry = memory.get_token_usage_telemetry(session_id)
    return {"telemetry": telemetry}

class ForkRequest(BaseModel):
    session_id: str
    fork_step_index: int

@app.post("/api/sessions/fork")
async def fork_session_endpoint(req: ForkRequest):
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
        
    import shutil
    old_agent_db = Path.home() / ".gemini" / "antigravity-cli" / "conversations" / f"{resolved_id}.db"
    new_agent_db = Path.home() / ".gemini" / "antigravity-cli" / "conversations" / f"{new_session_id}.db"
    if old_agent_db.exists():
        try:
            shutil.copy2(old_agent_db, new_agent_db)
        except Exception as e:
            print(f"[FORK] Warning: failed to copy agent DB file: {e}")

    return {"status": "success", "new_session_id": new_session_id}

@app.get("/api/telemetry/routes")
async def get_route_telemetry_endpoint():
    conn = get_connection(memory.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT route_name, status, COUNT(*) as count, AVG(latency) as avg_latency
            FROM route_telemetry
            GROUP BY route_name, status
        """)
        summary_rows = cursor.fetchall()

        cursor.execute("""
            SELECT id, session_id, route_name, model_name, status, error_message, latency, timestamp
            FROM route_telemetry
            ORDER BY timestamp DESC
            LIMIT 100
        """)
        recent_rows = cursor.fetchall()
        
        summary = []
        for r in summary_rows:
            summary.append({
                "route_name": r["route_name"],
                "status": r["status"],
                "count": r["count"],
                "avg_latency": r["avg_latency"]
            })
            
        recent = []
        for r in recent_rows:
            recent.append({
                "id": r["id"],
                "session_id": r["session_id"],
                "route_name": r["route_name"],
                "model_name": r["model_name"],
                "status": r["status"],
                "error_message": r["error_message"],
                "latency": r["latency"],
                "timestamp": r["timestamp"]
            })
            
        return {
            "status": "success",
            "summary": summary,
            "recent": recent
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}
    finally:
        conn.close()

@app.get("/api/quotas")
async def get_quotas():
    try:
        from agent.core.scheduler import fetch_real_quotas_sync
        await asyncio.to_thread(fetch_real_quotas_sync)
    except Exception:
        pass
    
    quotas = memory.get_model_quotas()
    if not quotas:
        return [
            {"model_family": "gemini", "pct_5h": 96.0, "pct_weekly": 89.0, "reset_5h": None, "reset_weekly": None, "last_updated": datetime.now(timezone.utc).isoformat()},
            {"model_family": "claude_gpt", "pct_5h": 100.0, "pct_weekly": 100.0, "reset_5h": None, "reset_weekly": None, "last_updated": datetime.now(timezone.utc).isoformat()}
        ]
    return quotas

@app.get("/api/tasks")
async def tasks_endpoint():
    tasks = memory.get_active_tasks()
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
                if any(t.get("id") == sub_id for t in tasks):
                    continue
                
                displayName = sub_id
                prompt_text = sub.get("prompt", "")
                profile_match = re.search(r'Spawning tooled subagent \(([^)]+)\)', prompt_text)
                if profile_match:
                    profile_name = profile_match.group(1)
                    if profile_name != 'generic':
                        displayName = profile_name.replace('_', ' ').title()
                
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

async def check_and_compact_session_history(session_id: str, model_name: str = "gemini-3.6-flash", api_key: Optional[str] = None) -> None:
    """Checks conversation history size and compacts oldest 40 rows into a summary if row count exceeds 60."""
    from agent.web import KeylessAgyAgent
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


