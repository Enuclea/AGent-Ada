import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent import memory
from agent.storage.db import get_connection

def match_cron_field(field_val: int, pattern: str, min_val: int, max_val: int) -> bool:
    if pattern == "*":
        return True
    
    # Handle steps like */5
    if pattern.startswith("*/"):
        try:
            step = int(pattern.split("/")[1])
            return field_val % step == 0
        except ValueError:
            return False
            
    # Handle lists like 1,2,3
    if "," in pattern:
        try:
            vals = map(int, pattern.split(","))
            return field_val in vals
        except ValueError:
            return False
            
    # Handle ranges like 1-5
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

def ensure_default_scheduled_tasks(conn=None):
    """Ensures that default scheduled background tasks are registered if not already present."""
    close_conn = False
    if conn is None:
        conn = get_connection(memory.DB_FILE_PATH)
        close_conn = True
    try:
        cursor = conn.cursor()
        
        default_tasks = []
        
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

async def execute_scheduled_task(name: str, prompt: str):
    """Executes a scheduled task using an isolated agent instance (not the shared default)."""
    from agent.core.plugins import _custom_scheduled_task_handlers
    if name in _custom_scheduled_task_handlers:
        try:
            await _custom_scheduled_task_handlers[name](prompt)
        except Exception as e:
            print(f"[Scheduled Task: {name}] Custom handler error: {e}")
        return

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

    if name == "Gmail Email Check":
        conversation_id = f"sched-gmail-email-check-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        memory.log_conversation_step(conversation_id, "user", f"[Scheduled Task: {name}] {prompt}")
        try:
            from enuclea.gmail_tool import sync_gmail_emails
            has_enuclea = True
        except ImportError:
            has_enuclea = False

        if has_enuclea:
            try:
                res = await sync_gmail_emails()
                memory.log_conversation_step(conversation_id, "assistant", res)
                print(f"[Scheduled Task: {name}] Executed directly. Result: {res}")
                return
            except Exception as e:
                err_msg = f"Failed to execute Gmail sync: {e}"
                print(f"[Scheduled Task: {name}] Error: {err_msg}")
                memory.log_conversation_step(conversation_id, "assistant", err_msg)
                return

    # Generic scheduled tasks: use a dedicated, isolated KeylessAgyAgent
    from agent.keyless import KeylessAgyAgent, TaskPriority

    priority = TaskPriority.SCHEDULED_CRITICAL if "Grace" in name else TaskPriority.SCHEDULED_ROUTINE
    conversation_id = f"sched-{name.lower().replace(' ', '-')}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    try:
        from agent.core.registry import tool_registry
        profile_name = name.lower().replace(" ", "_").replace("-", "_")
        specialist_inst = tool_registry.resolve_subagent_profile(profile_name)
        if not specialist_inst:
            specialist_inst = tool_registry.resolve_subagent_profile(name)
            
        system_instructions = specialist_inst or f"You are executing the scheduled background task: {name}. Complete it and report results concisely."

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
    # Ensure only one container instance runs the scheduler loop.
    import fcntl
    try:
        _scheduler_lock_file = open('/data/run_scheduler.lock', 'w')
        fcntl.lockf(_scheduler_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        print("[SCHEDULER] Another instance is already running the scheduler. Exiting.")
        return

    await asyncio.sleep(2)

    # Startup cleanup: mark stale tasks and pre-existing completions as processed
    try:
        conn = get_connection(memory.DB_FILE_PATH)
        cursor = conn.cursor()
        now_iso = datetime.now(timezone.utc).isoformat()
        # Mark any tasks left in 'running' state (from prior crash/restart) as failed
        cursor.execute("UPDATE active_tasks SET status = 'failed', completed_at = ? WHERE status = 'running'", (now_iso,))
        stale_count = cursor.rowcount
        if stale_count:
            print(f"[SCHEDULER] Startup cleanup: marked {stale_count} stale running tasks as failed")
        # Mark all existing subagent completion messages as processed to prevent re-firing
        cursor.execute("""
            SELECT DISTINCT subagent_id FROM subagent_messages
            WHERE role = 'subagent' AND (
                LOWER(message) LIKE '%subagent completed:%' OR LOWER(message) LIKE '%subagent failed:%'
            )
        """)
        for row in cursor.fetchall():
            cursor.execute(
                "INSERT OR IGNORE INTO processed_subagents (subagent_id, processed_at) VALUES (?, ?)",
                (row[0], now_iso)
            )
        conn.commit()
        conn.close()
        print("[SCHEDULER] Startup cleanup complete")
    except Exception as cleanup_err:
        print(f"[SCHEDULER] Startup cleanup error (non-fatal): {cleanup_err}")

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
                
                # We need to access active_subagents for checking status. We can import it from core.subagent_manager.
                from agent.core.subagent_manager import active_subagents
                
                resumed_sessions = set()
                for step_id, plan_id, step_desc, session_id, step_order in delegated_steps:
                    if session_id in resumed_sessions:
                        continue
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
                            cursor.execute("UPDATE plan_steps SET status = 'completed' WHERE plan_id = ? AND status = 'delegated'", (plan_id,))
                            conn.commit()
                            print(f"[SCHEDULER] Subagent {subagent_id} completed. All delegated steps for plan {plan_id} marked completed.")
                            
                            if session_id not in resumed_sessions:
                                resumed_sessions.add(session_id)
                                async def resume_parent(sess_id, sub_id, msg):
                                    if str(sess_id).startswith("benchmark-"):
                                        print(f"[SCHEDULER] Skipping resume for benchmark session {sess_id}")
                                        return
                                    # Check if the parent session is currently locked (already processing)
                                    try:
                                        from agent.api.router import get_session_lock, session_locks
                                        lock_key = sess_id
                                        # Also check discord session mappings for the lookup key
                                        mem = memory.load_memory()
                                        session_mappings = mem.get("key_value", {}).get("session_mappings", {})
                                        if isinstance(session_mappings, dict):
                                            reversed_map = {v: k for k, v in session_mappings.items()}
                                            lock_key = reversed_map.get(sess_id, sess_id)
                                        if lock_key in session_locks and session_locks[lock_key]._locked:
                                            print(f"[SCHEDULER] Session {sess_id} is currently locked (active request). Skipping resume.")
                                            return
                                    except Exception:
                                        pass
                                    try:
                                        channel_id = None
                                        mem = memory.load_memory()
                                        session_mappings = mem.get("key_value", {}).get("session_mappings", {})
                                        if isinstance(session_mappings, dict):
                                            reversed_map = {v: k for k, v in session_mappings.items()}
                                            original_discord_id = reversed_map.get(sess_id)
                                            if original_discord_id:
                                                channel_id = original_discord_id.replace("discord-session-", "").replace("discord-roleplay-", "")
                                        
                                        if not channel_id:
                                            # Not from Discord, trigger standard local HTTP dashboard client reload/resume
                                            import httpx
                                            from agent.core.internal_auth import get_internal_api_headers
                                            # Get loopback port
                                            port = int(os.environ.get("PORT", "8000"))
                                            payload = {
                                                "prompt": f"[SYSTEM RESUME] Step completed successfully. Output:\n{msg}",
                                                "session_id": sess_id
                                            }
                                            headers = get_internal_api_headers("POST", "/api/chat", json_data=payload)
                                            async with httpx.AsyncClient() as client:
                                                await client.post(f"http://localhost:{port}/api/chat", json=payload, headers=headers)
                                        else:
                                             # Mapped discord channel, trigger the bot API to resume the session through discord
                                             import httpx
                                             from agent.execution.tools.discord_tools import get_bot_api_headers
                                             bot_port = int(os.environ.get("DISCORD_BOT_PORT", "8090"))
                                             path = "/api/discord/resume"
                                             parts = sub_id.split("-")
                                             profile = parts[1] if len(parts) >= 2 and parts[0] == "subagent" else None
                                             payload = {
                                                 "channel_id": str(channel_id),
                                                 "session_id": sess_id,
                                                 "prompt": f"[SYSTEM RESUME] Step completed successfully. Output:\n{msg}",
                                                 "agent_profile": profile
                                             }
                                             headers = get_bot_api_headers("POST", path, json_data=payload)
                                             async with httpx.AsyncClient() as client:
                                                 resp = await client.post(f"http://127.0.0.1:{bot_port}{path}", json=payload, headers=headers)
                                                 if resp.status_code != 200:
                                                     print(f"[SCHEDULER] Failed to resume via Discord Bot API: HTTP {resp.status_code}: {resp.text}")
                                    except Exception as re_err:
                                        print(f"[SCHEDULER] Failed to resume parent session {sess_id}: {re_err}")
                                asyncio.create_task(resume_parent(session_id, subagent_id, message))
                                
                        elif "subagent failed:" in message.lower():
                            cursor.execute("UPDATE plan_steps SET status = 'failed', error_message = ? WHERE plan_id = ? AND status = 'delegated'", (message, plan_id))
                            conn.commit()
                            print(f"[SCHEDULER] Subagent {subagent_id} failed. Step {step_id} marked failed.")
                            
                            if session_id not in resumed_sessions:
                                resumed_sessions.add(session_id)
                                async def resume_parent_fail(sess_id, sub_id, msg):
                                    if str(sess_id).startswith("benchmark-"):
                                        print(f"[SCHEDULER] Skipping resume for benchmark session {sess_id}")
                                        return
                                    try:
                                        from agent.api.router import session_locks
                                        mem = memory.load_memory()
                                        session_mappings = mem.get("key_value", {}).get("session_mappings", {})
                                        lock_key = sess_id
                                        if isinstance(session_mappings, dict):
                                            reversed_map = {v: k for k, v in session_mappings.items()}
                                            lock_key = reversed_map.get(sess_id, sess_id)
                                        if lock_key in session_locks and session_locks[lock_key]._locked:
                                            print(f"[SCHEDULER] Session {sess_id} is currently locked. Skipping fail resume.")
                                            return
                                    except Exception:
                                        pass
                                    try:
                                        channel_id = None
                                        mem = memory.load_memory()
                                        session_mappings = mem.get("key_value", {}).get("session_mappings", {})
                                        if isinstance(session_mappings, dict):
                                            reversed_map = {v: k for k, v in session_mappings.items()}
                                            original_discord_id = reversed_map.get(sess_id)
                                            if original_discord_id:
                                                channel_id = original_discord_id.replace("discord-session-", "").replace("discord-roleplay-", "")
                                                
                                        if not channel_id:
                                            import httpx
                                            from agent.core.internal_auth import get_internal_api_headers
                                            port = int(os.environ.get("PORT", "8000"))
                                            payload = {
                                                "prompt": f"[SYSTEM RESUME] Step failed. Output:\n{msg}",
                                                "session_id": sess_id
                                            }
                                            headers = get_internal_api_headers("POST", "/api/chat", json_data=payload)
                                            async with httpx.AsyncClient() as client:
                                                await client.post(f"http://localhost:{port}/api/chat", json=payload, headers=headers)
                                        else:
                                             # Mapped discord channel, trigger the bot API to resume the session through discord
                                             import httpx
                                             from agent.execution.tools.discord_tools import get_bot_api_headers
                                             bot_port = int(os.environ.get("DISCORD_BOT_PORT", "8090"))
                                             path = "/api/discord/resume"
                                             parts = sub_id.split("-")
                                             profile = parts[1] if len(parts) >= 2 and parts[0] == "subagent" else None
                                             payload = {
                                                 "channel_id": str(channel_id),
                                                 "session_id": sess_id,
                                                 "prompt": f"[SYSTEM RESUME] Step failed. Output:\n{msg}",
                                                 "agent_profile": profile
                                             }
                                             headers = get_bot_api_headers("POST", path, json_data=payload)
                                             async with httpx.AsyncClient() as client:
                                                 resp = await client.post(f"http://127.0.0.1:{bot_port}{path}", json=payload, headers=headers)
                                                 if resp.status_code != 200:
                                                     print(f"[SCHEDULER] Failed to resume via Discord Bot API: HTTP {resp.status_code}: {resp.text}")
                                    except Exception as re_err:
                                        print(f"[SCHEDULER] Failed to resume parent session {sess_id} on failure: {re_err}")
                                asyncio.create_task(resume_parent_fail(session_id, subagent_id, message))

                # Check for active non-plan subagents to resume
                cursor.execute("""
                    SELECT DISTINCT parent_session_id 
                    FROM subagent_messages 
                    WHERE parent_session_id IS NOT NULL AND parent_session_id != 'New Session'
                """)
                active_parents = [r[0] for r in cursor.fetchall()]
                for parent_session_id in active_parents:
                    if parent_session_id in resumed_sessions:
                        continue
                    # Check if parent is NOT executing a plan
                    cursor.execute("SELECT count(*) FROM session_plans WHERE session_id = ?", (parent_session_id,))
                    if cursor.fetchone()[0] > 0:
                        continue
                        
                    cursor.execute("""
                        SELECT subagent_id, message, timestamp 
                        FROM subagent_messages 
                        WHERE parent_session_id = ? AND role = 'subagent'
                        ORDER BY id DESC LIMIT 1
                    """, (parent_session_id,))
                    subagent_row = cursor.fetchone()
                    if subagent_row:
                        subagent_id, message, timestamp = subagent_row
                        # Check if message is a completion/failure and hasn't been processed
                        is_completed = "subagent completed:" in message.lower()
                        is_failed = "subagent failed:" in message.lower()
                        
                        if is_completed or is_failed:
                            # Verify if we already resumed (check system logs for a resume prompt)
                            cursor.execute("""
                                SELECT count(*) FROM processed_subagents 
                                WHERE subagent_id = ?
                            """, (subagent_id,))
                            already_resumed = cursor.fetchone()[0] > 0
                            
                            if not already_resumed:
                                resumed_sessions.add(parent_session_id)
                                cursor.execute(
                                    "INSERT OR IGNORE INTO processed_subagents (subagent_id, processed_at) VALUES (?, ?)",
                                    (subagent_id, datetime.now(timezone.utc).isoformat())
                                )
                                conn.commit()
                                
                                async def resume_parent_non_plan(sess_id, sub_id, msg):
                                    if str(sess_id).startswith("benchmark-"):
                                        print(f"[SCHEDULER] Skipping resume for benchmark session {sess_id}")
                                        return
                                    try:
                                        from agent.api.router import session_locks
                                        mem = memory.load_memory()
                                        session_mappings = mem.get("key_value", {}).get("session_mappings", {})
                                        lock_key = sess_id
                                        if isinstance(session_mappings, dict):
                                            reversed_map = {v: k for k, v in session_mappings.items()}
                                            lock_key = reversed_map.get(sess_id, sess_id)
                                        if lock_key in session_locks and session_locks[lock_key]._locked:
                                            print(f"[SCHEDULER] Session {sess_id} is currently locked. Skipping non-plan resume.")
                                            return
                                    except Exception:
                                        pass
                                    try:
                                        channel_id = None
                                        mem = memory.load_memory()
                                        session_mappings = mem.get("key_value", {}).get("session_mappings", {})
                                        if isinstance(session_mappings, dict):
                                            reversed_map = {v: k for k, v in session_mappings.items()}
                                            original_discord_id = reversed_map.get(sess_id)
                                            if original_discord_id:
                                                channel_id = original_discord_id.replace("discord-session-", "").replace("discord-roleplay-", "")
                                                
                                        if not channel_id:
                                            import httpx
                                            from agent.core.internal_auth import get_internal_api_headers
                                            port = int(os.environ.get("PORT", "8000"))
                                            payload = {
                                                "prompt": f"[SYSTEM RESUME] Subagent task finished. Output:\n{msg}",
                                                "session_id": sess_id
                                            }
                                            headers = get_internal_api_headers("POST", "/api/chat", json_data=payload)
                                            async with httpx.AsyncClient() as client:
                                                await client.post(f"http://localhost:{port}/api/chat", json=payload, headers=headers)
                                        else:
                                             # Mapped discord channel, trigger the bot API to resume the session through discord
                                             import httpx
                                             from agent.execution.tools.discord_tools import get_bot_api_headers
                                             bot_port = int(os.environ.get("DISCORD_BOT_PORT", "8090"))
                                             path = "/api/discord/resume"
                                             parts = sub_id.split("-")
                                             profile = parts[1] if len(parts) >= 2 and parts[0] == "subagent" else None
                                             payload = {
                                                 "channel_id": str(channel_id),
                                                 "session_id": sess_id,
                                                 "prompt": f"[SYSTEM RESUME] Subagent task finished. Output:\n{msg}",
                                                 "agent_profile": profile
                                             }
                                             headers = get_bot_api_headers("POST", path, json_data=payload)
                                             async with httpx.AsyncClient() as client:
                                                 resp = await client.post(f"http://127.0.0.1:{bot_port}{path}", json=payload, headers=headers)
                                                 if resp.status_code != 200:
                                                     print(f"[SCHEDULER] Failed to resume via Discord Bot API: HTTP {resp.status_code}: {resp.text}")
                                    except Exception as re_err:
                                        print(f"[SCHEDULER] Failed to resume non-plan parent session {sess_id}: {re_err}")
                                        
                                asyncio.create_task(resume_parent_non_plan(parent_session_id, subagent_id, message))

                # Stale subagent detection: mark delegated steps as failed if no messages for >10 minutes
                try:
                    stale_threshold_minutes = 10
                    cursor.execute("""
                        SELECT ps.id, ps.plan_id, ps.description, sp.session_id, ps.step_order
                        FROM plan_steps ps
                        JOIN session_plans sp ON ps.plan_id = sp.id
                        WHERE ps.status = 'delegated'
                    """)
                    still_delegated = cursor.fetchall()
                    for step_id, plan_id, step_desc, session_id, step_order in still_delegated:
                        if session_id in resumed_sessions:
                            continue
                        # Check latest subagent message timestamp
                        cursor.execute("""
                            SELECT timestamp FROM subagent_messages
                            WHERE parent_session_id = ? AND role = 'subagent'
                            ORDER BY id DESC LIMIT 1
                        """, (session_id,))
                        msg_row = cursor.fetchone()
                        if msg_row:
                            try:
                                last_ts = datetime.fromisoformat(msg_row[0])
                                if last_ts.tzinfo is None:
                                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                                age_minutes = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
                                if age_minutes > stale_threshold_minutes:
                                    print(f"[SCHEDULER] Stale subagent detected for session {session_id}: {age_minutes:.0f}m since last message. Marking failed.")
                                    cursor.execute(
                                        "UPDATE plan_steps SET status = 'failed', error_message = ? WHERE plan_id = ? AND status = 'delegated'",
                                        (f"Stale subagent: no progress for {age_minutes:.0f} minutes", plan_id)
                                    )
                                    conn.commit()
                                    resumed_sessions.add(session_id)
                                    # Trigger a failure resume so Ada knows
                                    stale_msg = f"Subagent stalled after {age_minutes:.0f} minutes with no progress. Step: {step_desc}"
                                    async def resume_stale(sess_id, msg):
                                        if str(sess_id).startswith("benchmark-"):
                                            return
                                        try:
                                            from agent.api.router import session_locks
                                            _mem = memory.load_memory()
                                            _sm = _mem.get("key_value", {}).get("session_mappings", {})
                                            _lk = sess_id
                                            if isinstance(_sm, dict):
                                                _rv = {v: k for k, v in _sm.items()}
                                                _lk = _rv.get(sess_id, sess_id)
                                            if _lk in session_locks and session_locks[_lk]._locked:
                                                return
                                        except Exception:
                                            pass
                                        try:
                                            channel_id = None
                                            _mem2 = memory.load_memory()
                                            _sm2 = _mem2.get("key_value", {}).get("session_mappings", {})
                                            if isinstance(_sm2, dict):
                                                _rv2 = {v: k for k, v in _sm2.items()}
                                                _did = _rv2.get(sess_id)
                                                if _did:
                                                    channel_id = _did.replace("discord-session-", "").replace("discord-roleplay-", "")
                                            if not channel_id:
                                                import httpx
                                                port = int(os.environ.get("PORT", "8000"))
                                                headers = {}
                                                dp = os.environ.get("DASHBOARD_PASSWORD")
                                                if dp:
                                                    headers["Authorization"] = f"Bearer {dp}"
                                                async with httpx.AsyncClient() as client:
                                                    await client.post(f"http://localhost:{port}/api/chat", json={
                                                        "prompt": f"[SYSTEM RESUME] Subagent stalled and was terminated.\n{msg}",
                                                        "session_id": sess_id
                                                    }, headers=headers)
                                            else:
                                                import httpx
                                                from agent.execution.tools.discord_tools import get_bot_api_headers
                                                bot_port = int(os.environ.get("DISCORD_BOT_PORT", "8090"))
                                                path = "/api/discord/resume"
                                                payload = {
                                                    "channel_id": str(channel_id),
                                                    "session_id": sess_id,
                                                    "prompt": f"[SYSTEM RESUME] Subagent stalled and was terminated.\n{msg}"
                                                }
                                                headers = get_bot_api_headers("POST", path, json_data=payload)
                                                async with httpx.AsyncClient() as client:
                                                    await client.post(f"http://127.0.0.1:{bot_port}{path}", json=payload, headers=headers)
                                        except Exception as stale_err:
                                            print(f"[SCHEDULER] Failed to resume stale session {sess_id}: {stale_err}")
                                    asyncio.create_task(resume_stale(session_id, stale_msg))
                            except (ValueError, TypeError):
                                pass
                except Exception as stale_err:
                    print(f"[SCHEDULER] Error checking stale subagents: {stale_err}")

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

def discover_language_server():
    import os
    import re

    # Check environment variable first
    ls_address = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
    if ls_address:
        parts = ls_address.split(":")
        if len(parts) == 2:
            port_str = parts[1]
            if port_str.isdigit():
                csrf_token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
                return None, csrf_token, [int(port_str)]

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
    owner_uid = None
    try:
        owner_uid = os.stat(f"/proc/{pid}").st_uid
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
                socket_uid = int(parts[7])
                
                is_match = False
                if state == "0A":  # LISTEN
                    if inodes and inode in inodes:
                        is_match = True
                    elif not inodes and owner_uid is not None and socket_uid == owner_uid:
                        is_match = True
                        
                if is_match:
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
    if token is None or not ports:
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
                    reset_5h = None
                    reset_weekly = None
                    for bucket in group.get("buckets", []):
                        window = bucket.get("window")
                        rem = bucket.get("remainingFraction", 1.0)
                        pct_val = rem * 100.0
                        reset_time = bucket.get("resetTime")
                        if window == "5h":
                            pct_5h = pct_val
                            reset_5h = reset_time
                        elif window == "weekly":
                            pct_weekly = pct_val
                            reset_weekly = reset_time
                    
                    if pct_5h is not None and pct_weekly is not None:
                        memory.update_model_quotas(family, pct_5h, pct_weekly, reset_5h, reset_weekly)
        except Exception:
            pass
    return True

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
