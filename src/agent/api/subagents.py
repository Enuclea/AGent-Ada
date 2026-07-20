import asyncio
import os
import uuid
from pathlib import Path
from typing import Optional, List
from pydantic import BaseModel
from fastapi import HTTPException

from agent.api.router import app, get_session_lock
from agent import memory
from agent.storage.db import get_connection
from agent.core.subagent_manager import active_subagents, setup_sandbox_sync

class SpawnSubagentRequest(BaseModel):
    parent_session_id: Optional[str] = None
    subagent_id: str
    prompt: str
    target_files: Optional[List[str]] = None
    stub_files: Optional[List[str]] = None
    agent_profile: Optional[str] = None

@app.post("/api/subagents/spawn")
async def spawn_subagent_endpoint(req: SpawnSubagentRequest):
    sandbox_id = str(uuid.uuid4())
    sandbox_dir = Path("/tmp") / f"subagent_sandbox_{sandbox_id}"
    await asyncio.to_thread(sandbox_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(sandbox_dir.chmod, 0o700)
    
    current_workspace = os.getcwd()
    
    # Run sandbox filesystem setup in a worker thread to keep the event loop non-blocking
    await asyncio.to_thread(
        setup_sandbox_sync,
        current_workspace,
        sandbox_dir,
        req.target_files,
        req.stub_files
    )
                
    active_subagents[req.subagent_id] = {
        "task": None,
        "parent_session_id": req.parent_session_id,
        "agent": None,
        "response": None
    }
    
    # Register the subagent as an active task so it appears in the activity feed immediately
    display_name = req.agent_profile.replace('_', ' ').title() if req.agent_profile else "Subagent"
    task_id = f"task-agent-{req.subagent_id}"
    
    prompt_first_line = req.prompt.strip().split("\n")[0].strip()
    if prompt_first_line.startswith("Hi "):
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
            
            # Subagent KeylessAgyAgent instantiation
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

    async def watchdog_timer():
        """Watchdog: log progress markers every 60s and kill hung subagents after 300s of silence."""
        last_message_check = ""
        silence_start = asyncio.get_event_loop().time()
        
        while req.subagent_id in active_subagents:
            await asyncio.sleep(60)
            if req.subagent_id not in active_subagents:
                break
            
            # Check for latest message
            try:
                msgs = memory.get_subagent_messages(req.subagent_id)
                latest_msg = msgs[-1]["message"] if msgs else ""
                if latest_msg != last_message_check:
                    last_message_check = latest_msg
                    silence_start = asyncio.get_event_loop().time()
                else:
                    silence_duration = asyncio.get_event_loop().time() - silence_start
                    if silence_duration > 300:
                        print(f"[WATCHDOG] Subagent {req.subagent_id} has been silent for {silence_duration:.0f}s. Terminating.")
                        sub_info = active_subagents.get(req.subagent_id)
                        if sub_info:
                            # Kill the subprocess if it exists
                            resp = sub_info.get("response")
                            if resp and hasattr(resp, 'proc') and resp.proc and resp.proc.returncode is None:
                                try:
                                    resp.proc.kill()
                                    await resp.proc.wait()
                                except Exception:
                                    pass
                            # Cancel the task
                            if sub_info.get("task"):
                                sub_info["task"].cancel()
                        memory.log_subagent_message(req.subagent_id, "subagent", 
                            f"Subagent failed: Watchdog terminated after {silence_duration:.0f}s of inactivity")
                        memory.update_active_task_status(task_id, "failed")
                        active_subagents.pop(req.subagent_id, None)
                        break
                    else:
                        # Log a progress marker
                        print(f"[WATCHDOG] Subagent {req.subagent_id} still running ({silence_duration:.0f}s since last update)")
            except Exception as e:
                print(f"[WATCHDOG] Error checking subagent {req.subagent_id}: {e}")
            
    task = asyncio.create_task(run_subagent_background())
    active_subagents[req.subagent_id]["task"] = task
    # Start watchdog timer in background (fire-and-forget)
    asyncio.create_task(watchdog_timer())
    return {"status": "success", "sandbox_dir": str(sandbox_dir)}

@app.get("/api/subagents/{subagent_id}/messages")
async def get_subagent_messages_endpoint(subagent_id: str):
    messages = memory.get_subagent_messages(subagent_id)
    return {"messages": messages}

@app.get("/api/subagents")
async def list_subagents_endpoint():
    subagents = memory.get_subagents_status()
    return {"subagents": subagents}

@app.post("/api/sessions/{session_id}/cancel")
async def cancel_session(session_id: str):
    resolved_id = session_id
    if session_id.startswith("discord-session-"):
        mem = memory.load_memory()
        session_mappings = mem.get("key_value", {}).get("session_mappings", {})
        if isinstance(session_mappings, dict):
            resolved_id = session_mappings.get(session_id, session_id)
            
    # Cancel all active subagents for this parent session
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
        print(f"[CANCEL API] Error updating DB on cancel: {db_err}")
    finally:
        conn.close()
        
    lock = get_session_lock(session_id)
    if lock._locked:
        lock.release()
        
    return {"status": "success", "message": "Execution stopped.", "cancelled_subagents": cancelled_subs}
