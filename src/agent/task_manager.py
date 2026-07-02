"""Active task tracking, scheduled tasks, session plans, and Discord approval bridge.

Extracted from memory.py — covers task lifecycle, cron scheduling, plan execution,
and the approval workflow.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import agent.db as _db


def add_active_task(task_id: str, name: str, details: str) -> None:
    """Adds a new active task or tool execution to the database."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        started_at = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            """
            INSERT OR REPLACE INTO active_tasks (id, name, details, started_at, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, name, details, started_at, "running")
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def update_active_task_status(task_id: str, status: str) -> None:
    """Updates the status of an active task (e.g. 'completed', 'failed')."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        completed_at = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "UPDATE active_tasks SET status = ?, completed_at = ? WHERE id = ?",
            (status, completed_at, task_id)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def get_active_tasks() -> List[Dict[str, Any]]:
    """Retrieves all active running tasks and recently completed/failed tasks (within last 15 seconds)."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    results = []
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name, details, started_at, status, completed_at FROM active_tasks WHERE status = 'running' ORDER BY started_at DESC"
        )
        rows = cursor.fetchall()
        for row in rows:
            results.append({
                "id": row[0],
                "name": row[1],
                "details": row[2],
                "started_at": row[3],
                "status": row[4],
                "completed_at": row[5]
            })
            
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()
        cursor.execute(
            """
            SELECT id, name, details, started_at, status, completed_at 
            FROM active_tasks 
            WHERE status IN ('completed', 'failed', 'denied') AND (completed_at >= ? OR (completed_at IS NULL AND started_at >= ?))
            ORDER BY completed_at DESC, started_at DESC
            """,
            (cutoff, cutoff)
        )
        rows = cursor.fetchall()
        for row in rows:
            results.append({
                "id": row[0],
                "name": row[1],
                "details": row[2],
                "started_at": row[3],
                "status": row[4],
                "completed_at": row[5]
            })
    except Exception:
        pass
    finally:
        conn.close()
    return results

def get_active_task_status(task_id: str) -> Optional[str]:
    """Retrieves the status of a specific task by ID."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM active_tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        if row:
            return row[0]
    except Exception:
        pass
    finally:
        conn.close()
    return None

def clear_active_tasks() -> None:
    """Clears or completes all active tasks (e.g. at startup)."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE active_tasks SET status = 'completed' WHERE status = 'running'")
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def add_task_log(task_id: str, message: str) -> None:
    """Appends a progress log message for an active task/subagent."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        timestamp = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "INSERT INTO task_logs (task_id, timestamp, message) VALUES (?, ?, ?)",
            (task_id, timestamp, message)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def get_task_logs(task_id: str) -> List[Dict[str, Any]]:
    """Retrieves all log messages for a specific task ordered by timestamp."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    results = []
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT timestamp, message FROM task_logs WHERE task_id = ? ORDER BY timestamp ASC",
            (task_id,)
        )
        rows = cursor.fetchall()
        for row in rows:
            results.append({
                "timestamp": row[0],
                "message": row[1]
            })
    except Exception:
        pass
    finally:
        conn.close()
    return results


# --- Scheduled Tasks ---

def add_scheduled_task(task_id: str, name: str, prompt: str, cron_expr: str, next_run: str) -> None:
    """Creates a new scheduled background task/cron job."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO scheduled_tasks (id, name, prompt, cron_expr, next_run, last_run, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, name, prompt, cron_expr, next_run, None, "active")
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def get_scheduled_tasks() -> List[Dict[str, Any]]:
    """Retrieves all active scheduled tasks."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    results = []
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name, prompt, cron_expr, next_run, last_run, status FROM scheduled_tasks ORDER BY id DESC"
        )
        rows = cursor.fetchall()
        for row in rows:
            results.append({
                "id": row[0],
                "name": row[1],
                "prompt": row[2],
                "cron_expr": row[3],
                "next_run": row[4],
                "last_run": row[5],
                "status": row[6]
            })
    except Exception:
        pass
    finally:
        conn.close()
    return results

def update_scheduled_task_run(task_id: str, last_run: str, next_run: str) -> None:
    """Updates the execution runtimes of a scheduled task."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE scheduled_tasks SET last_run = ?, next_run = ? WHERE id = ?",
            (last_run, next_run, task_id)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def delete_scheduled_task(task_id: str) -> None:
    """Removes a scheduled task."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def ensure_plugin_scheduled_task(name: str, prompt: str, cron_expr: str) -> None:
    """Helper for plugins to register a default scheduled task in the database."""
    import uuid
    
    try:
        from agent.web import get_next_cron_run
        next_run_dt = get_next_cron_run(cron_expr, datetime.now(timezone.utc))
        next_run = next_run_dt.isoformat()
    except Exception:
        from datetime import timedelta
        next_run = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

    conn = sqlite3.connect(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM scheduled_tasks WHERE name = ?", (name,))
        count = cursor.fetchone()[0]
        if count == 0:
            schedule_id = "plugin-task-" + str(uuid.uuid4())
            cursor.execute(
                "INSERT INTO scheduled_tasks (id, name, prompt, cron_expr, next_run, last_run, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (schedule_id, name, prompt, cron_expr, next_run, None, "active")
            )
            conn.commit()
            print(f"[PLUGINS] Registered default scheduled task '{name}' in database.")
    except Exception as e:
        print(f"[PLUGINS] Failed to register default scheduled task '{name}': {e}")
    finally:
        conn.close()


# --- Session Plans ---

def add_session_plan(
    plan_id: str,
    session_id: str,
    title: str,
    status: str = "pending",
    goal: Optional[str] = None,
    acceptance_criteria: Optional[str] = None,
    non_goals: Optional[str] = None
) -> None:
    """Adds a new execution plan for a session."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        created_at = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            """
            INSERT OR REPLACE INTO session_plans 
            (id, session_id, title, status, created_at, goal, acceptance_criteria, non_goals) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (plan_id, session_id, title, status, created_at, goal, acceptance_criteria, non_goals)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def add_plan_step(
    step_id: str,
    plan_id: str,
    step_order: int,
    description: str,
    status: str = "pending",
    assigned_tool: Optional[str] = None,
    assigned_args: Optional[str] = None
) -> None:
    """Adds a step to an existing plan."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO plan_steps 
            (id, plan_id, step_order, description, status, assigned_tool, assigned_args, error_message) 
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (step_id, plan_id, step_order, description, status, assigned_tool, assigned_args)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def update_plan_step_status(step_id: str, status: str, error_message: Optional[str] = None) -> None:
    """Updates the execution status of a plan step."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE plan_steps SET status = ?, error_message = ? WHERE id = ?",
            (status, error_message, step_id)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def get_session_plan(session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves the active plan and its steps for a session."""
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, status, created_at, goal, acceptance_criteria, non_goals FROM session_plans WHERE session_id = ? ORDER BY created_at DESC LIMIT 1", (session_id,))
        plan_row = cursor.fetchone()
        if not plan_row:
            return None
        
        plan_id = plan_row[0]
        cursor.execute(
            "SELECT id, step_order, description, status, assigned_tool, assigned_args, error_message FROM plan_steps WHERE plan_id = ? ORDER BY step_order ASC",
            (plan_id,)
        )
        step_rows = cursor.fetchall()
        
        steps = []
        for r in step_rows:
            steps.append({
                "id": r[0],
                "step_order": r[1],
                "description": r[2],
                "status": r[3],
                "assigned_tool": r[4],
                "assigned_args": r[5],
                "error_message": r[6]
            })
            
        return {
            "id": plan_id,
            "title": plan_row[1],
            "status": plan_row[2],
            "created_at": plan_row[3],
            "goal": plan_row[4],
            "acceptance_criteria": plan_row[5],
            "non_goals": plan_row[6],
            "steps": steps
        }
    except Exception:
        return None
    finally:
        conn.close()


# --- Discord Approval Bridge ---

async def ask_discord_approval(task_id: str, tool_name: str, tool_args: str) -> None:
    """Posts a tool approval request to the Discord #control-room channel with buttons."""
    import aiohttp
    
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        env_path = Path(os.environ.get("DISCORD_ENV_PATH", "discord/.env"))
        if not env_path.is_absolute():
            env_path = Path(__file__).parent.parent.parent / env_path
        if env_path.exists():
            with open(env_path, "r") as f:
                for line in f:
                    if line.startswith("DISCORD_BOT_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break

    if not token:
        print(f"[APPROVAL] No Discord bot token found.")
        return

    channel_id = 1518056970538586272
    config_path_str = os.environ.get("DISCORD_CONFIG_PATH", "discord/config.json")
    config_path = Path(config_path_str)
    if not config_path.is_absolute():
        config_path = Path(__file__).parent.parent.parent / config_path
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                for cid, info in config.get("channels", {}).items():
                    if info.get("channel_name") == "control-room":
                        channel_id = int(cid)
                        break
        except Exception:
            pass

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "embeds": [{
            "title": "🔔 Tool Confirmation Required",
            "description": f"The agent is proposing to execute the following tool in a background task:\n\n"
                           f"**Tool:** `{tool_name}`\n"
                           f"**Arguments:**\n```json\n{tool_args}\n```",
            "color": 16776960,
            "fields": [
                {"name": "Task ID", "value": f"`{task_id}`", "inline": True}
            ]
        }],
        "components": [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "label": "Approve",
                        "style": 3,
                        "custom_id": f"approve_{task_id}"
                    },
                    {
                        "type": 2,
                        "label": "Deny (with feedback)",
                        "style": 4,
                        "custom_id": f"deny_{task_id}"
                    }
                ]
            }
        ]
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    print(f"[APPROVAL] Failed to send approval request: {resp.status} - {text}")
        except Exception as e:
            print(f"[APPROVAL] Exception sending approval request: {e}")
