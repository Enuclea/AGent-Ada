import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MEMORY_FILE_PATH = Path.home() / ".agent" / "memory.json"

def get_memory_file() -> Path:
    """Returns the path to the memory file and ensures its parent directory exists."""
    MEMORY_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return MEMORY_FILE_PATH

def load_memory() -> Dict[str, Any]:
    """Loads the persistent memory JSON file.
    
    Returns:
        A dictionary containing 'facts' (list) and 'key_value' (dict).
    """
    path = get_memory_file()
    if not path.exists():
        return {"facts": [], "key_value": {}}
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            data.setdefault("facts", [])
            data.setdefault("key_value", {})
            return data
    except (json.JSONDecodeError, OSError):
        # Fallback in case of corruption
        return {"facts": [], "key_value": {}}

def save_memory(memory: Dict[str, Any]) -> None:
    """Saves the memory state to the persistent JSON file."""
    path = get_memory_file()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(memory, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Warning: Failed to save persistent memory: {e}")

def add_fact(fact: str) -> str:
    """Appends a new fact to the persistent facts list."""
    mem = load_memory()
    facts: List[str] = mem["facts"]
    if fact not in facts:
        facts.append(fact)
        save_memory(mem)
        return f"Successfully added fact to persistent memory: '{fact}'"
    return f"Fact already exists in persistent memory: '{fact}'"

def update_key_value(key: str, value: Any) -> str:
    """Updates or sets a key-value pair in persistent memory."""
    mem = load_memory()
    mem["key_value"][key] = value
    save_memory(mem)
    return f"Successfully set memory key '{key}' to '{value}'"

def get_fact_summary() -> str:
    """Generates a text summary of the persistent memory.
    
    This is formatted for injection into the agent's system instructions.
    """
    mem = load_memory()
    facts = mem.get("facts", [])
    kv = mem.get("key_value", {})
    
    if not facts and not kv:
        return ""
        
    lines = ["\n[PERSISTENT MEMORY FROM PAST SESSIONS]"]
    if facts:
        lines.append("Remembered facts/notes:")
        for fact in facts:
            lines.append(f"  - {fact}")
    if kv:
        lines.append("Key-value settings/data:")
        for k, v in kv.items():
            lines.append(f"  - {k}: {v}")
    lines.append("[END OF PERSISTENT MEMORY]\n")
    return "\n".join(lines)


# --- SQLite Conversation History & FTS5 Search Database ---

DB_FILE_PATH = Path.home() / ".agent" / "history.db"

def init_db() -> None:
    """Initializes the SQLite conversation history and FTS5 search virtual tables."""
    DB_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        # Main step logs
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversation_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TEXT,
            role TEXT,
            content TEXT,
            tool_name TEXT,
            tool_result TEXT
        )
        """)
        # FTS5 virtual table
        cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS conversation_search USING fts5(
            step_id UNINDEXED,
            session_id,
            role,
            content,
            tool_name
        )
        """)
        # Active tasks tracking
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_tasks (
            id TEXT PRIMARY KEY,
            name TEXT,
            details TEXT,
            started_at TEXT,
            status TEXT,
            completed_at TEXT
        )
        """)
        try:
            cursor.execute("ALTER TABLE active_tasks ADD COLUMN completed_at TEXT")
        except sqlite3.OperationalError:
            pass
        # Task progress logs
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            timestamp TEXT,
            message TEXT
        )
        """)
        # Scheduled tasks
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id TEXT PRIMARY KEY,
            name TEXT,
            prompt TEXT,
            cron_expr TEXT,
            next_run TEXT,
            last_run TEXT,
            status TEXT
        )
        """)
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def log_conversation_step(
    session_id: str,
    role: str,
    content: str,
    tool_name: Optional[str] = None,
    tool_result: Optional[str] = None
) -> None:
    """Logs a conversation step to SQLite and indexes it in FTS5."""
    if not session_id:
        session_id = "New Session"
    timestamp = datetime.now(timezone.utc).isoformat()
    
    conn = sqlite3.connect(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO conversation_steps (session_id, timestamp, role, content, tool_name, tool_result)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, timestamp, role, content, tool_name, tool_result)
        )
        step_id = cursor.lastrowid
        
        cursor.execute(
            """
            INSERT INTO conversation_search (step_id, session_id, role, content, tool_name)
            VALUES (?, ?, ?, ?, ?)
            """,
            (step_id, session_id, role, content, tool_name)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def search_conversations(query: str) -> List[Dict[str, Any]]:
    """Performs full-text search (FTS5) over past conversations.
    
    Falls back to LIKE search if FTS5 query format fails or is unsupported.
    """
    conn = sqlite3.connect(DB_FILE_PATH)
    results = []
    try:
        cursor = conn.cursor()
        try:
            # FTS5 search
            cursor.execute(
                """
                SELECT session_id, role, content, tool_name 
                FROM conversation_search 
                WHERE conversation_search MATCH ?
                ORDER BY rank LIMIT 50
                """,
                (query,)
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            # Fallback LIKE query
            like_query = f"%{query}%"
            cursor.execute(
                """
                SELECT session_id, role, content, tool_name 
                FROM conversation_steps 
                WHERE content LIKE ? OR tool_name LIKE ?
                ORDER BY id DESC LIMIT 50
                """,
                (like_query, like_query)
            )
            rows = cursor.fetchall()
            
        for row in rows:
            results.append({
                "session_id": row[0],
                "role": row[1],
                "content": row[2],
                "tool_name": row[3]
            })
    except Exception:
        pass
    finally:
        conn.close()
    return results

def add_active_task(task_id: str, name: str, details: str) -> None:
    """Adds a new active task or tool execution to the database."""
    conn = sqlite3.connect(DB_FILE_PATH)
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
    conn = sqlite3.connect(DB_FILE_PATH)
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
    conn = sqlite3.connect(DB_FILE_PATH)
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

def clear_active_tasks() -> None:
    """Clears or completes all active tasks (e.g. at startup)."""
    conn = sqlite3.connect(DB_FILE_PATH)
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
    conn = sqlite3.connect(DB_FILE_PATH)
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
    conn = sqlite3.connect(DB_FILE_PATH)
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

def add_scheduled_task(task_id: str, name: str, prompt: str, cron_expr: str, next_run: str) -> None:
    """Creates a new scheduled background task/cron job."""
    conn = sqlite3.connect(DB_FILE_PATH)
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
    conn = sqlite3.connect(DB_FILE_PATH)
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
    conn = sqlite3.connect(DB_FILE_PATH)
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
    conn = sqlite3.connect(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

active_session_id = None

def compact_all_memories() -> Dict[str, Any]:
    """Compacts persistent memories in memory.json and history.db to free up space.
    
    1. Deduplicates memory.json facts (exact matches case-insensitive, and subsets).
    2. Prunes completed or failed tasks in active_tasks, keeping only the 100 most recent ones.
    3. Prunes orphaned task_logs whose task_id is no longer kept in active_tasks.
    4. Runs SQLite VACUUM to reclaim disk space.
    """
    stats = {
        "memory_json_before_facts": 0,
        "memory_json_after_facts": 0,
        "db_size_before": 0,
        "db_size_after": 0,
        "active_tasks_before": 0,
        "active_tasks_after": 0,
        "task_logs_deleted": 0,
    }
    
    # --- 1. Compact memory.json facts ---
    mem = load_memory()
    facts = mem.get("facts", [])
    stats["memory_json_before_facts"] = len(facts)
    
    unique_facts = []
    seen_normalized = set()
    for f in facts:
        norm = f.strip().lower()
        if not norm:
            continue
        # Deduplicate exact or very similar facts
        if norm not in seen_normalized:
            seen_normalized.add(norm)
            unique_facts.append(f)
            
    # Optional subset/superset cleaning of facts
    final_facts = []
    for f in unique_facts:
        norm = f.strip().lower()
        # If there's another fact in unique_facts that contains this fact entirely and is longer and more descriptive,
        # we consider this one redundant.
        is_sub = False
        for other in unique_facts:
            other_norm = other.strip().lower()
            if norm != other_norm and norm in other_norm and len(other_norm) > len(norm) + 10:
                is_sub = True
                break
        if not is_sub:
            final_facts.append(f)
            
    mem["facts"] = final_facts
    save_memory(mem)
    stats["memory_json_after_facts"] = len(final_facts)
    
    # --- 2. Compact history.db ---
    if DB_FILE_PATH.exists():
        stats["db_size_before"] = DB_FILE_PATH.stat().st_size
        
        conn = sqlite3.connect(DB_FILE_PATH)
        try:
            cursor = conn.cursor()
            
            # --- Get initial counts ---
            cursor.execute("SELECT count(*) FROM active_tasks")
            stats["active_tasks_before"] = cursor.fetchone()[0]
            
            # --- Prune active_tasks (keep 100 most recent completed/failed tasks) ---
            cursor.execute("""
                SELECT id FROM active_tasks 
                WHERE status IN ('completed', 'failed') 
                ORDER BY started_at DESC LIMIT 100
            """)
            keep_ids = [row[0] for row in cursor.fetchall()]
            
            if keep_ids:
                placeholders = ",".join("?" for _ in keep_ids)
                cursor.execute(f"""
                    DELETE FROM active_tasks 
                    WHERE status IN ('completed', 'failed') AND id NOT IN ({placeholders})
                """, keep_ids)
            else:
                cursor.execute("DELETE FROM active_tasks WHERE status IN ('completed', 'failed')")
                
            cursor.execute("SELECT count(*) FROM active_tasks")
            stats["active_tasks_after"] = cursor.fetchone()[0]
            
            # --- Prune task_logs (orphans) ---
            cursor.execute("DELETE FROM task_logs WHERE task_id NOT IN (SELECT id FROM active_tasks)")
            stats["task_logs_deleted"] = cursor.rowcount
            
            pass
            
            conn.commit()
            cursor.execute("VACUUM")
        except Exception as e:
            print(f"Error during SQLite compaction: {e}")
        finally:
            conn.close()
            
        stats["db_size_after"] = DB_FILE_PATH.stat().st_size
        
    # Record the last compaction completion time in persistent memory
    try:
        update_key_value("last_compaction", datetime.now(timezone.utc).isoformat())
    except Exception:
        pass
        
    return stats

# Initialize database on module load
try:
    init_db()
except Exception:
    pass
