import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import urllib.request

DB_PATH = Path.home() / ".agent" / "history.db"

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
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Find running tasks
        cursor.execute("SELECT id, name, details, started_at FROM active_tasks WHERE status = 'running'")
        running_tasks = cursor.fetchall()
        
        if not running_tasks:
            # Output clean JSON or simple text
            print(json.dumps({
                "status": "ok",
                "running_count": 0,
                "stalled_count": 0,
                "report": "No active tasks are currently running."
            }))
            return
            
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        report_entries = []
        stalled_count = 0
        stalled_tasks = []
        
        markdown_lines = [
            "# Grace's Timekeeper Report",
            f"Generated at: {now.isoformat()} UTC\n"
        ]
        
        stalled_section = []
        running_section = []
        
        for task_id, name, details, started_at in running_tasks:
            # Find the last log entry for this task
            cursor.execute("SELECT timestamp, message FROM task_logs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,))
            last_log = cursor.fetchone()
            
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
                    inactive_minutes = inactive.seconds // 60
                    last_update_str = f"{inactive_minutes}m ago: {log_msg}"
                except Exception:
                    pass
            else:
                # If no log yet, inactivity is based on started_at
                try:
                    start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00")).replace(tzinfo=None)
                    inactive = now - start_dt
                    inactive_minutes = inactive.seconds // 60
                except Exception:
                    pass
            
            is_stalled = inactive_minutes > inactivity_threshold_mins
            if is_stalled:
                stalled_count += 1
                
            task_info = {
                "id": task_id,
                "name": name,
                "details": details,
                "elapsed": elapsed_str,
                "last_update": last_update_str,
                "is_stalled": is_stalled,
                "inactive_minutes": inactive_minutes
            }
            report_entries.append(task_info)
            if is_stalled:
                stalled_tasks.append(task_info)
            
            # Build sections for MD report
            status_tag = "⚠️ STALLED" if is_stalled else "🟢 RUNNING"
            task_md = (
                f"- **{name}** [{status_tag}]\n"
                f"  - *ID*: `{task_id}`\n"
                f"  - *Details*: `{details}`\n"
                f"  - *Elapsed Time*: {elapsed_str}\n"
                f"  - *Last Update*: {last_update_str}"
            )
            if is_stalled:
                stalled_section.append(task_md)
            else:
                running_section.append(task_md)
        
        if stalled_section:
            markdown_lines.append("## ⚠️ WARNING: Potential Halted/Delayed Tasks Detected")
            markdown_lines.extend(stalled_section)
            markdown_lines.append("")
            
        markdown_lines.append("## Running Tasks Overview")
        if running_section:
            markdown_lines.extend(running_section)
        else:
            markdown_lines.append("- No non-stalled active tasks running.")
            
        report_md = "\n".join(markdown_lines)
        
        # If there are stalled tasks, send a direct alert to Discord
        if stalled_tasks:
            alert_lines = [
                "🚨 **Ada Timekeeper Warning: Hung Task Detected!**",
                "The following active task(s) appear to be delayed or stalled:"
            ]
            for t in stalled_tasks:
                alert_lines.append(
                    f"• **Task**: `{t['name']}`\n"
                    f"  - **ID**: `{t['id']}`\n"
                    f"  - **Inactivity**: {t['inactive_minutes']} minutes without progress\n"
                    f"  - **Last Log**: {t['last_update']}"
                )
            alert_lines.append("\n_Action required. Please direct mention me or use the appropriate prefix to check logs/terminate._")
            send_discord_alert("\n".join(alert_lines))
            
        # Output clean JSON containing both structure and pre-rendered markdown
        print(json.dumps({
            "status": "warning" if stalled_count > 0 else "ok",
            "running_count": len(running_tasks),
            "stalled_count": stalled_count,
            "tasks": report_entries,
            "report": report_md
        }, indent=2))
        
    except Exception as e:
        print(json.dumps({"error": f"Error checking tasks: {e}"}))
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    check_tasks()
