import sqlite3
from datetime import datetime, timezone
import json
import sys
import urllib.request

from agent.storage.db import get_connection, DB_FILE_PATH
DB_PATH = DB_FILE_PATH

# Import shared Discord notification utilities
try:
    from agent.observability.notifications import send_discord_alert, send_direct_discord_message, get_discord_config
except ImportError:
    # Fallback for standalone execution
    def send_discord_alert(text, channel_name="control-room"):
        print(f"[GRACE] Discord notification not available (standalone mode): {text[:100]}...")
        return False
    def send_direct_discord_message(channel_id, text):
        print(f"[GRACE] Direct Discord message not available (standalone mode): {channel_id}: {text[:100]}...")
        return False
    def get_discord_config():
        return {}

import re
import os
import hmac
import hashlib
import time

def get_agent_api_base():
    return os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8000").rstrip("/")

def get_auth_headers(method: str, path: str, query: str = "", body: bytes = b""):
    secret_str = os.environ.get("INTERNAL_API_SECRET", "")
    if not secret_str:
        dashboard_password = os.environ.get("DASHBOARD_PASSWORD", "admin")
        secret = hashlib.sha256(dashboard_password.encode()).digest()
    else:
        secret = hashlib.sha256(secret_str.encode()).digest()
        
    timestamp_str = str(int(time.time()))
    body_hash = hashlib.sha256(body).hexdigest()
    
    message = f"{method.upper()}:{path}:{query}:{timestamp_str}:{body_hash}".encode()
    sig = hmac.new(secret, message, hashlib.sha256).hexdigest()
    
    return {
        "X-Signature": sig,
        "X-Timestamp": timestamp_str,
        "Content-Type": "application/json"
    }

def wake_agent_up(session_id, prompt, agent_profile=None):
    import json
    import sys
    api_base = get_agent_api_base()
    path = "/api/chat"
    url = f"{api_base}{path}"
    
    payload = {
        "session_id": session_id,
        "prompt": prompt
    }
    if agent_profile:
        payload["agent_profile"] = agent_profile

    try:
        body = json.dumps(payload).encode("utf-8")
        headers = get_auth_headers("POST", path, body=body)
        
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST"
        )
        # 45 second timeout for quick agent check-in response
        with urllib.request.urlopen(req, timeout=45.0) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            return res_data
    except Exception as e:
        print(f"[GRACE] Error waking up agent session {session_id}: {e}", file=sys.stderr)
        return None

def get_agent_display_name(profile_name):
    if not profile_name:
        return "Ada"
    profile = str(profile_name).lower()
    if "lacie" in profile:
        return "Lacie"
    if "val" in profile or "qa" in profile:
        return "Val"
    if "kira" in profile or "ops" in profile:
        return "Kira"
    if "grace" in profile or "timekeeper" in profile:
        return "Grace"
    if "observer" in profile:
        return "Observer"
    return profile_name.capitalize()

def get_agent_name_for_session(session_id, cursor=None):
    if not session_id:
        return "Ada"
    
    if cursor:
        try:
            cursor.execute("SELECT name FROM active_tasks WHERE id = ?", (session_id,))
            row = cursor.fetchone()
            if row and row[0]:
                return get_agent_display_name(row[0])
        except Exception:
            pass
            
        try:
            cursor.execute("SELECT title FROM session_plans WHERE session_id = ?", (session_id,))
            row = cursor.fetchone()
            if row and row[0]:
                return get_agent_display_name(row[0])
        except Exception:
            pass
            
    if "lacie" in session_id.lower():
        return "Lacie"
    if "val" in session_id.lower():
        return "Val"
    if "kira" in session_id.lower():
        return "Kira"
    
    return "Ada"

def get_profile_from_subagent_id(subagent_id):
    if not subagent_id:
        return None
    parts = subagent_id.split("-")
    if len(parts) >= 2 and parts[0] == "subagent":
        return parts[1]
    return None

def get_channel_id_from_session(session_id):
    if not session_id:
        return None
    match = re.search(r"discord-[a-z]+-(\d+)", str(session_id))
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return None

def get_control_room_channel_id():
    try:
        config = get_discord_config()
        for cid, info in config.get("channels", {}).items():
            if isinstance(info, dict) and info.get("channel_name") == "control-room":
                return int(cid)
    except Exception:
        pass
    return 1518056970538586272 # Fallback

def check_tasks(max_follow_up_count=None, inactivity_threshold_mins=None):
    if inactivity_threshold_mins is None:
        env_val = os.environ.get("GRACE_INACTIVITY_THRESHOLD_MINS")
        if env_val:
            try:
                inactivity_threshold_mins = int(env_val)
            except ValueError:
                inactivity_threshold_mins = 30
        else:
            inactivity_threshold_mins = 30

    if max_follow_up_count is None:
        env_val = os.environ.get("GRACE_MAX_FOLLOW_UP_COUNT")
        if env_val:
            try:
                max_follow_up_count = int(env_val)
            except ValueError:
                max_follow_up_count = 2
        else:
            max_follow_up_count = 2

    # Warning interval duration resolves to a fraction of the total inactivity threshold
    warning_threshold_mins = max(1, inactivity_threshold_mins // (max_follow_up_count + 1))

    if not DB_PATH.exists():
        print(json.dumps({"error": "Database does not exist."}))
        return
        
    conn = get_connection(DB_PATH)
    cursor = conn.cursor()
    
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        # --- 1. Check Active Tasks ---
        cursor.execute("SELECT id, name, details, started_at FROM active_tasks WHERE status = 'running'")
        running_tasks = cursor.fetchall()
        
        stalled_tasks = []
        tasks_to_warn = []
        running_tasks_list = []
        
        last_logs = {}
        if running_tasks:
            task_ids = [t[0] for t in running_tasks]
            placeholders = ",".join("?" for _ in task_ids)
            query = f"""
            SELECT task_id, timestamp, message FROM (
                SELECT task_id, timestamp, message,
                       ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY id DESC) as rn
                FROM task_logs
                WHERE task_id IN ({placeholders})
            ) WHERE rn = 1
            """
            cursor.execute(query, task_ids)
            for task_id, timestamp, message in cursor.fetchall():
                last_logs[task_id] = (timestamp, message)

        for task_id, name, details, started_at in running_tasks:
            # Find the last log entry for this task
            last_log = last_logs.get(task_id)
            
            # Parse start time
            try:
                start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00")).replace(tzinfo=None)
                elapsed = now - start_dt
                elapsed_str = f"{elapsed.seconds // 60}m {elapsed.seconds % 60}s"
            except Exception:
                elapsed_str = "Unknown"
                
            last_update_str = "No updates logged yet"
            inactive_minutes = 9999
            
            if last_log:
                log_time, log_msg = last_log
                try:
                    log_dt = datetime.fromisoformat(log_time.replace("Z", "+00:00")).replace(tzinfo=None)
                    inactive = now - log_dt
                    inactive_minutes = inactive.seconds // 60 + (inactive.days * 24 * 60)
                    last_update_str = f"{inactive_minutes}m ago: {log_msg}"
                except Exception:
                    pass
            else:
                try:
                    start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00")).replace(tzinfo=None)
                    inactive = now - start_dt
                    inactive_minutes = inactive.seconds // 60 + (inactive.days * 24 * 60)
                except Exception:
                    pass
            
            # Count warnings already sent
            cursor.execute(
                "SELECT COUNT(*) FROM task_logs WHERE task_id = ? AND message LIKE 'Ada Timekeeper: Pinged agent for status check%'",
                (task_id,)
            )
            warning_count = cursor.fetchone()[0]
            already_warned = warning_count > 0
            
            is_stalled = False
            needs_warning = False
            
            if already_warned:
                if inactive_minutes >= warning_threshold_mins:
                    if warning_count >= max_follow_up_count:
                        is_stalled = True
                    else:
                        needs_warning = True
            else:
                if inactive_minutes >= warning_threshold_mins:
                    needs_warning = True
            
            task_info = {
                "id": task_id,
                "name": name,
                "details": details,
                "elapsed": elapsed_str,
                "last_update": last_update_str,
                "inactive_minutes": inactive_minutes,
                "warning_count": warning_count
            }
            
            if is_stalled:
                stalled_tasks.append(task_info)
            elif needs_warning:
                tasks_to_warn.append(task_info)
            else:
                running_tasks_list.append(task_info)
                 
        # --- 2. Check Subagents ---
        cursor.execute(
            "SELECT subagent_id, role, message, timestamp, parent_session_id FROM subagent_messages ORDER BY timestamp ASC"
        )
        all_messages = cursor.fetchall()
        
        from collections import defaultdict
        grouped_messages = defaultdict(list)
        for subagent_id, role, message, timestamp, parent_session_id in all_messages:
            grouped_messages[subagent_id].append((role, message, timestamp, parent_session_id))
            
        stalled_subagents = []
        subagents_to_warn = []
        running_subagents_list = []
        
        for sid, msgs in grouped_messages.items():
            if not msgs:
                continue
            
            parent_msg = next((m for m in msgs if m[0] == "parent"), None)
            subagent_msgs = [m for m in msgs if m[0] == "subagent"]
            last_sub_msg = subagent_msgs[-1] if subagent_msgs else None
            parent_session_id = next((m[3] for m in msgs if m[3]), None)
            
            prompt = ""
            if parent_msg:
                m_text = parent_msg[1]
                idx = m_text.find("with prompt: ")
                if idx != -1:
                    prompt = m_text[idx + 13:]
                else:
                    prompt = m_text
                started_at = parent_msg[2]
            else:
                started_at = msgs[0][2]
                
            status = "active"
            completed_at = None
            
            if last_sub_msg:
                completed_at = last_sub_msg[2]
                m_text = last_sub_msg[1]
                is_finished = False
                if m_text.startswith("[FAILED]") or m_text.startswith("[SUCCESS]"):
                    is_finished = True
                else:
                    for indicator in ["failed", "error", "completed", "terminated"]:
                        if indicator in m_text.lower():
                            if "Ada Timekeeper: Pinged agent" not in m_text:
                                is_finished = True
                                break
                if is_finished:
                    if m_text.startswith("[FAILED]"):
                        status = "failed"
                    elif m_text.startswith("[SUCCESS]"):
                        status = "completed"
                    else:
                        status = "failed" if ("failed" in m_text.lower() or "error" in m_text.lower() or "terminated" in m_text.lower()) else "completed"
            
            if status == "active":
                ref_time = last_sub_msg[2] if last_sub_msg else started_at
                try:
                    ref_dt = datetime.fromisoformat(ref_time.replace("Z", "+00:00")).replace(tzinfo=None)
                    elapsed = now - ref_dt
                    inactive_minutes = elapsed.seconds // 60 + (elapsed.days * 24 * 60)
                    elapsed_str = f"{elapsed.seconds // 60}m {elapsed.seconds % 60}s"
                except Exception:
                    inactive_minutes = 9999
                    elapsed_str = "Unknown"
                
                # Count warnings already sent
                cursor.execute(
                    "SELECT COUNT(*) FROM subagent_messages WHERE subagent_id = ? AND message LIKE 'Ada Timekeeper: Pinged agent for status check%'",
                    (sid,)
                )
                warning_count = cursor.fetchone()[0]
                already_warned = warning_count > 0
                
                is_stalled = False
                needs_warning = False
                
                if already_warned:
                    if inactive_minutes >= warning_threshold_mins:
                        if warning_count >= max_follow_up_count:
                            is_stalled = True
                        else:
                            needs_warning = True
                else:
                    if inactive_minutes >= warning_threshold_mins:
                        needs_warning = True
                
                sub_info = {
                    "subagent_id": sid,
                    "parent_session_id": parent_session_id,
                    "prompt": prompt,
                    "started_at": started_at,
                    "elapsed": elapsed_str,
                    "inactive_minutes": inactive_minutes,
                    "warning_count": warning_count
                }
                
                if is_stalled:
                    stalled_subagents.append(sub_info)
                elif needs_warning:
                    subagents_to_warn.append(sub_info)
                else:
                    running_subagents_list.append(sub_info)

        # --- 2b. Check Stalled Plans ---
        cursor.execute("SELECT id, session_id, title, created_at FROM session_plans WHERE status = 'running'")
        running_plans = cursor.fetchall()
        
        stalled_plans = []
        plans_to_warn = []
        for p_id, p_sid, p_title, p_created_at in running_plans:
            cursor.execute("SELECT timestamp, content FROM conversation_steps WHERE session_id = ? ORDER BY id DESC LIMIT 1", (p_sid,))
            step_row = cursor.fetchone()
            
            last_activity = p_created_at
            last_content = ""
            if step_row:
                if step_row[0]:
                    last_activity = step_row[0]
                if step_row[1]:
                    last_content = step_row[1]
                
            try:
                activity_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00")).replace(tzinfo=None)
                inactive = now - activity_dt
                inactive_minutes = inactive.seconds // 60 + (inactive.days * 24 * 60)
            except Exception:
                inactive_minutes = 9999
                
            # Count warnings already sent
            cursor.execute(
                "SELECT COUNT(*) FROM conversation_steps WHERE session_id = ? AND content LIKE 'Ada Timekeeper: Pinged agent for status check%'",
                (p_sid,)
            )
            warning_count = cursor.fetchone()[0]
            already_warned = warning_count > 0
            
            is_stalled = False
            needs_warning = False
            
            if already_warned:
                if inactive_minutes >= warning_threshold_mins:
                    if warning_count >= max_follow_up_count:
                        is_stalled = True
                    else:
                        needs_warning = True
            else:
                if inactive_minutes >= warning_threshold_mins:
                    needs_warning = True
                    
            plan_info = {
                "id": p_id,
                "session_id": p_sid,
                "title": p_title,
                "inactive_minutes": inactive_minutes,
                "warning_count": warning_count
            }
            
            if is_stalled:
                stalled_plans.append(plan_info)
            elif needs_warning:
                plans_to_warn.append(plan_info)

        # --- 2c. Check Stalled Discord Tasks ---
        stalled_discord_tasks = []
        discord_tasks_to_warn = []
        discord_db_path = DB_PATH.parent / "discord_queue.db"
        if discord_db_path.exists():
            try:
                discord_conn = sqlite3.connect(str(discord_db_path))
                discord_cursor = discord_conn.cursor()
                
                # Check / ensure 'warning_count' column exists
                try:
                    discord_cursor.execute("ALTER TABLE discord_tasks ADD COLUMN warning_count INTEGER DEFAULT 0")
                    discord_conn.commit()
                except sqlite3.OperationalError:
                    pass
                
                discord_cursor.execute("SELECT id, prompt_text, timestamp, warning_count, channel_id FROM discord_tasks WHERE status = 'processing'")
                for d_id, d_prompt, d_ts, d_warning_count, d_channel_id in discord_cursor.fetchall():
                    elapsed_sec = time.time() - d_ts
                    inactive_minutes = int(elapsed_sec / 60)
                    
                    is_stalled = False
                    needs_warning = False
                    
                    if d_warning_count > 0:
                        if inactive_minutes >= warning_threshold_mins:
                            if d_warning_count >= max_follow_up_count:
                                is_stalled = True
                            else:
                                needs_warning = True
                    else:
                        if inactive_minutes >= warning_threshold_mins:
                            needs_warning = True
                            
                    d_info = {
                        "id": d_id,
                        "prompt": d_prompt or "No prompt",
                        "inactive_minutes": inactive_minutes,
                        "channel_id": d_channel_id,
                        "warning_count": d_warning_count
                    }
                    
                    if is_stalled:
                        stalled_discord_tasks.append(d_info)
                    elif needs_warning:
                        discord_tasks_to_warn.append(d_info)
                discord_conn.close()
            except Exception as e:
                print(f"[GRACE] Error reading Discord tasks: {e}", file=sys.stderr)

        # --- 3. Process Warnings / Ping Agent & Trigger Wakeup ---
        control_room_channel = get_control_room_channel_id()
        
        if tasks_to_warn:
            for task in tasks_to_warn:
                new_warning_count = task["warning_count"] + 1
                cursor.execute(
                    "INSERT INTO task_logs (task_id, timestamp, message) VALUES (?, ?, ?)",
                    (task["id"], now.isoformat(), f"Ada Timekeeper: Pinged agent for status check - task open without response for {task['inactive_minutes']} minutes. (Warning {new_warning_count}/{max_follow_up_count})")
                )
                
                channel_id = control_room_channel
                agent_name = get_agent_display_name(task["name"])
                
                # Send non-intrusive status check message to the user's channel
                msg = f"🔍 **[Lacie] Investigating anomaly, asking {agent_name}...**"
                send_direct_discord_message(channel_id, msg)
                
                # Ping /api/chat to trigger the agent loop and prompt it to check in
                wake_prompt = f"Ada Timekeeper: Task '{task['name']}' has been inactive for {task['inactive_minutes']} minutes. Please check status and provide a brief update."
                wake_agent_up(task["id"], wake_prompt, agent_profile=task["name"])
                
        if subagents_to_warn:
            for sub in subagents_to_warn:
                new_warning_count = sub["warning_count"] + 1
                cursor.execute(
                    "INSERT INTO subagent_messages (subagent_id, role, message, timestamp, parent_session_id) VALUES (?, ?, ?, ?, ?)",
                    (sub["subagent_id"], "subagent", f"Ada Timekeeper: Pinged agent for status check - subagent open without response for {sub['inactive_minutes']} minutes. (Warning {new_warning_count}/{max_follow_up_count})", now.isoformat(), sub["parent_session_id"])
                )
                
                channel_id = get_channel_id_from_session(sub["parent_session_id"]) or control_room_channel
                agent_name = get_agent_name_for_session(sub["subagent_id"], cursor)
                
                msg = f"🔍 **[Lacie] Investigating anomaly, asking {agent_name}...**"
                send_direct_discord_message(channel_id, msg)
                
                profile = get_profile_from_subagent_id(sub["subagent_id"])
                wake_prompt = f"Ada Timekeeper: Subagent task has been inactive for {sub['inactive_minutes']} minutes. Please check status and provide a brief update."
                wake_agent_up(sub["subagent_id"], wake_prompt, agent_profile=profile)
                
        if plans_to_warn:
            for plan in plans_to_warn:
                new_warning_count = plan["warning_count"] + 1
                cursor.execute(
                    "INSERT INTO conversation_steps (session_id, timestamp, role, content) VALUES (?, ?, ?, ?)",
                    (plan["session_id"], now.isoformat(), "system", f"Ada Timekeeper: Pinged agent for status check - plan open without response for {plan['inactive_minutes']} minutes. (Warning {new_warning_count}/{max_follow_up_count})")
                )
                
                channel_id = get_channel_id_from_session(plan["session_id"]) or control_room_channel
                agent_name = get_agent_name_for_session(plan["session_id"], cursor)
                
                msg = f"🔍 **[Lacie] Investigating anomaly, asking {agent_name}...**"
                send_direct_discord_message(channel_id, msg)
                
                wake_prompt = f"Ada Timekeeper: Plan execution has been inactive for {plan['inactive_minutes']} minutes. Please check status and provide a brief update."
                wake_agent_up(plan["session_id"], wake_prompt)
                
        if discord_tasks_to_warn:
            try:
                discord_conn = sqlite3.connect(str(discord_db_path))
                discord_cursor = discord_conn.cursor()
                for d_task in discord_tasks_to_warn:
                    new_warning_count = d_task["warning_count"] + 1
                    # Update timestamp to reset inactivity counter for the warning interval, and increment count
                    discord_cursor.execute(
                        "UPDATE discord_tasks SET warning_count = ?, timestamp = ? WHERE id = ?",
                        (new_warning_count, time.time(), d_task["id"])
                    )
                    
                    channel_id = d_task["channel_id"] or control_room_channel
                    session_id = f"discord-session-{channel_id}"
                    agent_name = get_agent_name_for_session(session_id, cursor)
                    
                    msg = f"🔍 **[Lacie] Investigating anomaly, asking {agent_name}...**"
                    send_direct_discord_message(channel_id, msg)
                    
                    wake_prompt = f"Ada Timekeeper: Discord task processing has been inactive for {d_task['inactive_minutes']} minutes. Please check status and provide a brief update."
                    wake_agent_up(session_id, wake_prompt)
                discord_conn.commit()
                discord_conn.close()
            except Exception as e:
                print(f"[GRACE] Error updating warning_count in Discord tasks: {e}", file=sys.stderr)

        if tasks_to_warn or subagents_to_warn or plans_to_warn:
            conn.commit()

        # --- 4. Auto-termination / Cleanup ---
        cleaned_tasks = []
        cleaned_subagents = []
        cleaned_plans = []
        cleaned_discord_tasks = []
        
        if stalled_tasks:
            for task in stalled_tasks:
                cursor.execute(
                    "UPDATE active_tasks SET status = 'failed', completed_at = ? WHERE id = ?",
                    (now.isoformat(), task["id"])
                )
                cursor.execute(
                    "INSERT INTO task_logs (task_id, timestamp, message) VALUES (?, ?, ?)",
                    (task["id"], now.isoformat(), f"Task automatically terminated by Grace Monitor due to inactivity timeout (exceeded {max_follow_up_count} warnings).")
                )
                cleaned_tasks.append(task)
                
        if stalled_subagents:
            for sub in stalled_subagents:
                cursor.execute(
                    "INSERT INTO subagent_messages (subagent_id, role, message, timestamp) VALUES (?, ?, ?, ?)",
                    (sub["subagent_id"], "subagent", f"Subagent failed: Terminated automatically by Grace Monitor due to inactivity timeout (exceeded {max_follow_up_count} warnings).", now.isoformat())
                )
                cleaned_subagents.append(sub)

        if stalled_plans:
            for plan in stalled_plans:
                cursor.execute(
                    "UPDATE session_plans SET status = 'failed' WHERE id = ?",
                    (plan["id"],)
                )
                cursor.execute(
                    "UPDATE plan_steps SET status = 'failed', error_message = ? WHERE plan_id = ? AND status NOT IN ('completed', 'failed')",
                    (f"Terminated automatically by Grace Monitor due to inactivity timeout (exceeded {max_follow_up_count} warnings).", plan["id"])
                )
                cleaned_plans.append(plan)
                
        if stalled_tasks or stalled_subagents or stalled_plans:
            conn.commit()

        if stalled_discord_tasks:
            try:
                discord_conn = sqlite3.connect(str(discord_db_path))
                discord_cursor = discord_conn.cursor()
                for d_task in stalled_discord_tasks:
                    discord_cursor.execute("UPDATE discord_tasks SET status = 'failed' WHERE id = ?", (d_task["id"],))
                    cleaned_discord_tasks.append(d_task)
                discord_conn.commit()
                discord_conn.close()
            except Exception as e:
                print(f"[GRACE] Error updating Discord tasks: {e}", file=sys.stderr)
            
        # --- 5. Discord Alerts ---
        if cleaned_tasks or cleaned_subagents or cleaned_plans or cleaned_discord_tasks:
            alert_lines = [
                "🚨 **Ada Timekeeper: Auto-Cleaned Stalled Tasks/Subagents/Plans**",
                f"The following items exceeded the maximum follow-up count of {max_follow_up_count} warnings and were auto-terminated:"
            ]
            for t in cleaned_tasks:
                alert_lines.append(f"• **Task**: `{t['name']}` (ID: `{t['id']}` - inactive for {t['inactive_minutes']}m)")
            for s in cleaned_subagents:
                prompt_snippet = s['prompt'][:60] + "..." if len(s['prompt']) > 60 else s['prompt']
                alert_lines.append(f"• **Subagent**: `{prompt_snippet}` (ID: `{s['subagent_id']}` - inactive for {s['inactive_minutes']}m)")
            for p in stalled_plans:
                alert_lines.append(f"• **Plan**: `{p['title']}` (ID: `{p['id']}` - inactive for {p['inactive_minutes']}m)")
            for d in cleaned_discord_tasks:
                prompt_snippet = d['prompt'][:60] + "..." if len(d['prompt']) > 60 else d['prompt']
                alert_lines.append(f"• **Discord Task**: `{prompt_snippet}` (ID: `{d['id']}` - inactive for {d['inactive_minutes']}m)")
            
            send_discord_alert("\n".join(alert_lines))
            
        # --- 6. Generate Markdown Report ---
        markdown_lines = [
            "# Grace's Timekeeper Report",
            f"Generated at: {now.isoformat()} UTC\n"
        ]
        
        if cleaned_tasks or cleaned_subagents or cleaned_plans or cleaned_discord_tasks:
            markdown_lines.append("## ♻️ Auto-Cleaned Stalled Items (Terminated)")
            for t in cleaned_tasks:
                markdown_lines.append(f"- **Task**: `{t['name']}` (ID: `{t['id']}`) - Inactivity: {t['inactive_minutes']}m")
            for s in cleaned_subagents:
                markdown_lines.append(f"- **Subagent**: `{s['prompt']}` (ID: `{s['subagent_id']}`) - Inactivity: {s['inactive_minutes']}m")
            for p in cleaned_plans:
                markdown_lines.append(f"- **Plan**: `{p['title']}` (ID: `{p['id']}`) - Inactivity: {p['inactive_minutes']}m")
            for d in cleaned_discord_tasks:
                markdown_lines.append(f"- **Discord Task**: `{d['prompt']}` (ID: `{d['id']}`) - Inactivity: {d['inactive_minutes']}m")
            markdown_lines.append("")

        if tasks_to_warn or subagents_to_warn or plans_to_warn or discord_tasks_to_warn:
            markdown_lines.append("## ⚠️ Warned / Pinged Inactive Items")
            for t in tasks_to_warn:
                markdown_lines.append(f"- **Task**: `{t['name']}` (ID: `{t['id']}`) - Inactivity: {t['inactive_minutes']}m (Warning {t['warning_count'] + 1})")
            for s in subagents_to_warn:
                markdown_lines.append(f"- **Subagent**: `{s['prompt']}` (ID: `{s['subagent_id']}`) - Inactivity: {s['inactive_minutes']}m (Warning {s['warning_count'] + 1})")
            for p in plans_to_warn:
                markdown_lines.append(f"- **Plan**: `{p['title']}` (ID: `{p['id']}`) - Inactivity: {p['inactive_minutes']}m (Warning {p['warning_count'] + 1})")
            for d in discord_tasks_to_warn:
                markdown_lines.append(f"- **Discord Task**: `{d['prompt']}` (ID: `{d['id']}`) - Inactivity: {d['inactive_minutes']}m (Warning {d['warning_count'] + 1})")
            markdown_lines.append("")
            
        markdown_lines.append("## Active Running Tasks")
        if running_tasks_list:
            for t in running_tasks_list:
                markdown_lines.append(
                    f"- **{t['name']}**\n"
                    f"  - *ID*: `{t['id']}`\n"
                    f"  - *Details*: `{t['details']}`\n"
                    f"  - *Elapsed Time*: {t['elapsed']}\n"
                    f"  - *Last Update*: {t['last_update']}"
                )
        else:
            markdown_lines.append("- No running active tasks.")
            
        markdown_lines.append("\n## Active Running Subagents")
        if running_subagents_list:
            for s in running_subagents_list:
                markdown_lines.append(
                    f"- **Subagent (Prompt: {s['prompt']})**\n"
                    f"  - *ID*: `{s['subagent_id']}`\n"
                    f"  - *Elapsed Time*: {s['elapsed']}\n"
                    f"  - *Inactivity*: {s['inactive_minutes']}m"
                )
        else:
            markdown_lines.append("- No running active subagents.")
            
        report_md = "\n".join(markdown_lines)
        
        # Output clean JSON
        print(json.dumps({
            "status": "cleanup" if (cleaned_tasks or cleaned_subagents or cleaned_plans or cleaned_discord_tasks) else ("warning" if (tasks_to_warn or subagents_to_warn or plans_to_warn or discord_tasks_to_warn) else "ok"),
            "running_tasks_count": len(running_tasks_list),
            "cleaned_tasks_count": len(cleaned_tasks),
            "running_subagents_count": len(running_subagents_list),
            "cleaned_subagents_count": len(cleaned_subagents),
            "cleaned_plans_count": len(cleaned_plans),
            "cleaned_discord_tasks_count": len(cleaned_discord_tasks),
            "report": report_md
        }, indent=2))
        
    finally:
        conn.close()

if __name__ == "__main__":
    try:
        check_tasks()
    except Exception as e:
        print(json.dumps({"error": f"Error checking tasks: {e}"}))
        sys.exit(1)
