import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import urllib.request

from agent.storage.db import get_connection, DB_FILE_PATH
DB_PATH = DB_FILE_PATH

# Import shared Discord notification utilities
try:
    from agent.notifications import send_discord_alert
except ImportError:
    # Fallback for standalone execution
    def send_discord_alert(text, channel_name="control-room"):
        print(f"[GRACE] Discord notification not available (standalone mode): {text[:100]}...")
        return False

def check_tasks(inactivity_threshold_mins=10):
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
            
            is_stalled = inactive_minutes > inactivity_threshold_mins
            task_info = {
                "id": task_id,
                "name": name,
                "details": details,
                "elapsed": elapsed_str,
                "last_update": last_update_str,
                "is_stalled": is_stalled,
                "inactive_minutes": inactive_minutes
            }
            
            if is_stalled:
                stalled_tasks.append(task_info)
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
                # Check for explicit completion/failure messages
                is_finished = False
                if m_text.startswith("[FAILED]") or m_text.startswith("[SUCCESS]"):
                    is_finished = True
                else:
                    for indicator in ["failed", "error", "completed", "terminated"]:
                        if indicator in m_text.lower():
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
                try:
                    start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00")).replace(tzinfo=None)
                    elapsed = now - start_dt
                    inactive_minutes = elapsed.seconds // 60 + (elapsed.days * 24 * 60)
                    elapsed_str = f"{elapsed.seconds // 60}m {elapsed.seconds % 60}s"
                except Exception:
                    inactive_minutes = 9999
                    elapsed_str = "Unknown"
                
                sub_info = {
                    "subagent_id": sid,
                    "parent_session_id": parent_session_id,
                    "prompt": prompt,
                    "started_at": started_at,
                    "elapsed": elapsed_str,
                    "inactive_minutes": inactive_minutes
                }
                
                if inactive_minutes > inactivity_threshold_mins:
                    stalled_subagents.append(sub_info)
                else:
                    running_subagents_list.append(sub_info)

        # --- 3. Auto-termination / Cleanup ---
        cleaned_tasks = []
        cleaned_subagents = []
        
        if stalled_tasks:
            for task in stalled_tasks:
                cursor.execute(
                    "UPDATE active_tasks SET status = 'failed', completed_at = ? WHERE id = ?",
                    (now.isoformat(), task["id"])
                )
                cursor.execute(
                    "INSERT INTO task_logs (task_id, timestamp, message) VALUES (?, ?, ?)",
                    (task["id"], now.isoformat(), "Task automatically terminated by Grace Monitor due to inactivity timeout.")
                )
                cleaned_tasks.append(task)
                
        if stalled_subagents:
            for sub in stalled_subagents:
                cursor.execute(
                    "INSERT INTO subagent_messages (subagent_id, role, message, timestamp) VALUES (?, ?, ?, ?)",
                    (sub["subagent_id"], "subagent", "Subagent failed: Terminated automatically by Grace Monitor due to inactivity timeout (stalled).", now.isoformat())
                )
                cleaned_subagents.append(sub)
                
        if stalled_tasks or stalled_subagents:
            conn.commit()
            
        # --- 4. Discord Alerts ---
        if cleaned_tasks or cleaned_subagents:
            alert_lines = [
                "🚨 **Ada Timekeeper: Auto-Cleaned Stalled Tasks/Subagents**",
                "The following items exceeded inactivity thresholds and were auto-terminated to prevent resource leaks:"
            ]
            for t in cleaned_tasks:
                alert_lines.append(f"• **Task**: `{t['name']}` (ID: `{t['id']}` - inactive for {t['inactive_minutes']}m)")
            for s in cleaned_subagents:
                prompt_snippet = s['prompt'][:60] + "..." if len(s['prompt']) > 60 else s['prompt']
                alert_lines.append(f"• **Subagent**: `{prompt_snippet}` (ID: `{s['subagent_id']}` - inactive for {s['inactive_minutes']}m)")
            
            send_discord_alert("\n".join(alert_lines))
            
        # --- 5. Generate Markdown Report ---
        markdown_lines = [
            "# Grace's Timekeeper Report",
            f"Generated at: {now.isoformat()} UTC\n"
        ]
        
        if cleaned_tasks or cleaned_subagents:
            markdown_lines.append("## ♻️ Auto-Cleaned Stalled Items (Terminated)")
            for t in cleaned_tasks:
                markdown_lines.append(f"- **Task**: `{t['name']}` (ID: `{t['id']}`) - Inactivity: {t['inactive_minutes']}m")
            for s in cleaned_subagents:
                markdown_lines.append(f"- **Subagent**: `{s['prompt']}` (ID: `{s['subagent_id']}`) - Inactivity: {s['inactive_minutes']}m")
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
            "status": "cleanup" if (cleaned_tasks or cleaned_subagents) else "ok",
            "running_tasks_count": len(running_tasks_list),
            "cleaned_tasks_count": len(cleaned_tasks),
            "running_subagents_count": len(running_subagents_list),
            "cleaned_subagents_count": len(cleaned_subagents),
            "report": report_md
        }, indent=2))
        
    except Exception as e:
        print(json.dumps({"error": f"Error checking tasks: {e}"}))
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    check_tasks()
