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

DB_FILE_PATH = Path(os.getenv("AGENT_DB_PATH", str(Path.home() / ".agent" / "history.db")))

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
        # Roleplay memories
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS roleplay_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            key TEXT,
            fact TEXT,
            timestamp TEXT
        )
        """)
        # Session plans
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_plans (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            title TEXT,
            status TEXT,
            created_at TEXT
        )
        """)
        # Plan steps
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS plan_steps (
            id TEXT PRIMARY KEY,
            plan_id TEXT,
            step_order INTEGER,
            description TEXT,
            status TEXT,
            assigned_tool TEXT,
            assigned_args TEXT,
            error_message TEXT
        )
        """)
        # Token usage telemetry
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS token_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            model_name TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cost REAL,
            timestamp TEXT
        )
        """)
        # Subagent messages
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS subagent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subagent_id TEXT,
            role TEXT,
            message TEXT,
            timestamp TEXT
        )
        """)
        # Model quotas
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS model_quotas (
            model_family TEXT PRIMARY KEY,
            pct_5h REAL,
            pct_weekly REAL,
            last_updated TEXT
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

active_roleplay_session_id = None
active_session_id = None

def add_roleplay_memory(session_id: str, key: str, fact: str) -> None:
    """Adds a new fact to the roleplay memory table."""
    conn = sqlite3.connect(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        timestamp = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            """
            INSERT INTO roleplay_memories (session_id, key, fact, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, key, fact, timestamp)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def get_roleplay_memories(session_id: str) -> List[Dict[str, Any]]:
    """Retrieves all roleplay memories saved for a specific session/channel, including shared rumors and messages."""
    conn = sqlite3.connect(DB_FILE_PATH)
    results = []
    seen = set()
    
    def add_row(key, fact, timestamp):
        # normalize and check uniqueness of key-fact pair to prevent duplicates
        unique_key = (key.strip().lower(), fact.strip().lower())
        if unique_key not in seen:
            seen.add(unique_key)
            results.append({
                "key": key,
                "fact": fact,
                "timestamp": timestamp
            })

    try:
        cursor = conn.cursor()
        
        # 1. Fetch memories for the specific session_id
        cursor.execute(
            """
            SELECT key, fact, timestamp FROM roleplay_memories 
            WHERE session_id = ? 
            ORDER BY id ASC
            """,
            (session_id,)
        )
        for row in cursor.fetchall():
            add_row(row[0], row[1], row[2])
            
        # 2. Fetch all rumor, message, and Ada's backstory memories from ALL sessions (shared globally)
        cursor.execute(
            """
            SELECT key, fact, timestamp FROM roleplay_memories 
            WHERE key LIKE '%rumor%' OR key LIKE '%message%' OR key LIKE '%rumour%'
               OR LOWER(key) LIKE '%ada%past%' OR LOWER(key) LIKE '%ada%history%'
               OR LOWER(key) LIKE '%ada%backstory%' OR LOWER(key) LIKE '%ada%lore%'
            ORDER BY id ASC
            """
        )
        for row in cursor.fetchall():
            add_row(row[0], row[1], row[2])
            
        # 3. If this is a DM session or another channel, also pull memories of the main bar
        main_bar_session = "discord-roleplay-1518087367465111594"
        if session_id != main_bar_session:
            cursor.execute(
                """
                SELECT key, fact, timestamp FROM roleplay_memories 
                WHERE session_id = ? 
                ORDER BY id ASC
                """,
                (main_bar_session,)
            )
            for row in cursor.fetchall():
                add_row(row[0], row[1], row[2])
                
    except Exception as e:
        print(f"Error in get_roleplay_memories: {e}")
    finally:
        conn.close()
    return results

def compact_all_memories() -> Dict[str, Any]:
    """Compacts study/roleplay memories in memory.json and history.db to free up space.
    
    1. Deduplicates memory.json facts (exact matches case-insensitive, and subsets).
    2. Deduplicates roleplay_memories in SQLite database:
       - Removes exact duplicate entries of (session_id, key, fact).
       - Removes redundant non-main-bar session memories which identically exist in the main bar session.
    3. Prunes completed or failed tasks in active_tasks, keeping only the 100 most recent ones.
    4. Prunes orphaned task_logs whose task_id is no longer kept in active_tasks.
    5. Runs SQLite VACUUM to reclaim disk space.
    """
    stats = {
        "memory_json_before_facts": 0,
        "memory_json_after_facts": 0,
        "db_size_before": 0,
        "db_size_after": 0,
        "roleplay_memories_before": 0,
        "roleplay_memories_after": 0,
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
            cursor.execute("SELECT count(*) FROM roleplay_memories")
            stats["roleplay_memories_before"] = cursor.fetchone()[0]
            
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
            
            # --- Deduplicate and simplify roleplay_memories ---
            cursor.execute("SELECT id, session_id, key, fact, timestamp FROM roleplay_memories ORDER BY id ASC")
            all_rp = cursor.fetchall()
            
            # Separate main bar and others
            main_bar_session = "discord-roleplay-1518087367465111594"
            main_bar_facts = {} # (normalized_key, normalized_fact) -> id
            other_facts = []
            
            for row_id, sess_id, key, fact, ts in all_rp:
                norm_key = key.strip().lower()
                norm_fact = fact.strip().lower()
                if sess_id == main_bar_session:
                    main_bar_facts[(norm_key, norm_fact)] = row_id
                else:
                    other_facts.append((row_id, sess_id, norm_key, norm_fact))
                    
            # Delete redundant other memories that are exactly represented in the main bar session
            ids_to_delete = set()
            for row_id, sess_id, norm_key, norm_fact in other_facts:
                if (norm_key, norm_fact) in main_bar_facts:
                    ids_to_delete.add(row_id)
                    
            # Delete duplicates within the same session (keeps only the latest/highest ID row)
            seen_session_key_fact = set()
            for row_id, sess_id, key, fact, ts in reversed(all_rp):
                if row_id in ids_to_delete:
                    continue
                norm_key = key.strip().lower()
                norm_fact = fact.strip().lower()
                uniq = (sess_id, norm_key, norm_fact)
                if uniq in seen_session_key_fact:
                    ids_to_delete.add(row_id)
                else:
                    seen_session_key_fact.add(uniq)
                    
            if ids_to_delete:
                placeholders = ",".join("?" for _ in ids_to_delete)
                cursor.execute(f"DELETE FROM roleplay_memories WHERE id IN ({placeholders})", list(ids_to_delete))
                
            cursor.execute("SELECT count(*) FROM roleplay_memories")
            stats["roleplay_memories_after"] = cursor.fetchone()[0]
            
            conn.commit()
            cursor.execute("VACUUM")
        except Exception as e:
            print(f"Error during SQLite compaction: {e}")
        finally:
            conn.close()
            
        stats["db_size_after"] = DB_FILE_PATH.stat().st_size
        
    try:
        update_key_value("last_compaction", datetime.now(timezone.utc).isoformat())
    except Exception:
        pass
        
    return stats

def get_active_task_status(task_id: str) -> Optional[str]:
    """Retrieves the status of a specific task by ID."""
    conn = sqlite3.connect(DB_FILE_PATH)
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

def get_auto_rag_context(prompt: Optional[str]) -> str:
    """Runs a FTS search on past conversations for the given prompt under the hood,
    returning a formatted string of the top 3 most relevant historical interactions.
    """
    import re
    if not prompt:
        return ""
        
    clean_query = " OR ".join(re.findall(r"\w+", prompt))
    if not clean_query:
        return ""

    results = []
    try:
        results = search_conversations(clean_query)
    except Exception:
        try:
            results = search_conversations(prompt)
        except Exception:
            pass

    if not results:
        return ""

    lines = []
    seen_content = set()
    count = 0
    for res in results:
        content = res["content"].strip()
        if not content or content in seen_content:
            continue
        seen_content.add(content)
        
        role = res["role"].upper()
        tool_desc = f" (Tool Call: {res['tool_name']})" if res["tool_name"] else ""
        truncated = content
        if len(truncated) > 300:
            truncated = truncated[:300] + "... [truncated]"
            
        lines.append(f"- **Role:** {role}{tool_desc}\n  **Content:** {truncated}")
        count += 1
        if count >= 3:
            break

    if not lines:
        return ""

    return "[AUTO-RAG: RELEVANT HISTORICAL INTERACTIONS]\n" + "\n".join(lines) + "\n[END OF AUTO-RAG]"

async def ask_discord_approval(task_id: str, tool_name: str, tool_args: str) -> None:
    """Posts a tool approval request to the Discord #control-room channel with buttons."""
    import aiohttp
    import json
    
    token = None
    env_path = Path(__file__).parent.parent.parent / "discord" / ".env"
    if env_path.exists():
        with open(env_path, "r") as f:
            for line in f:
                if line.startswith("DISCORD_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    if not token:
        print(f"[APPROVAL] No Discord bot token found at {env_path}.")
        return

    channel_id = 1518056970538586272
    config_path = Path(__file__).parent.parent.parent / "discord" / "config.json"
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
            "color": 16776960,  # Yellow
            "fields": [
                {"name": "Task ID", "value": f"`{task_id}`", "inline": True}
            ]
        }],
        "components": [
            {
                "type": 1,  # Action Row
                "components": [
                    {
                        "type": 2,  # Button
                        "label": "Approve",
                        "style": 3,  # Success (green)
                        "custom_id": f"approve_{task_id}"
                    },
                    {
                        "type": 2,  # Button
                        "label": "Deny (with feedback)",
                        "style": 4,  # Danger (red)
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

# --- Hermes DB Helpers ---

def add_session_plan(plan_id: str, session_id: str, title: str, status: str = "pending") -> None:
    """Adds a new execution plan for a session."""
    conn = sqlite3.connect(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        created_at = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "INSERT OR REPLACE INTO session_plans (id, session_id, title, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (plan_id, session_id, title, status, created_at)
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
    conn = sqlite3.connect(DB_FILE_PATH)
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
    conn = sqlite3.connect(DB_FILE_PATH)
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
    conn = sqlite3.connect(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, status, created_at FROM session_plans WHERE session_id = ? ORDER BY created_at DESC LIMIT 1", (session_id,))
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
            "steps": steps
        }
    except Exception:
        return None
    finally:
        conn.close()

def log_token_usage(session_id: str, model_name: str, input_tokens: int, output_tokens: int, cost: float) -> None:
    """Records token usage and cost calculation in the telemetry logs."""
    conn = sqlite3.connect(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        timestamp = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "INSERT INTO token_telemetry (session_id, model_name, input_tokens, output_tokens, cost, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, model_name, input_tokens, output_tokens, cost, timestamp)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def get_token_usage_telemetry(session_id: str) -> List[Dict[str, Any]]:
    """Retrieves token usage telemetry records for a session."""
    conn = sqlite3.connect(DB_FILE_PATH)
    results = []
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, model_name, input_tokens, output_tokens, cost, timestamp FROM token_telemetry WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,)
        )
        rows = cursor.fetchall()
        for r in rows:
            results.append({
                "id": r[0],
                "model_name": r[1],
                "input_tokens": r[2],
                "output_tokens": r[3],
                "cost": r[4],
                "timestamp": r[5]
            })
    except Exception:
        pass
    finally:
        conn.close()
    return results

def log_subagent_message(subagent_id: str, role: str, message: str) -> None:
    """Records a messaging communication log between parent and subagent."""
    conn = sqlite3.connect(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        timestamp = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "INSERT INTO subagent_messages (subagent_id, role, message, timestamp) VALUES (?, ?, ?, ?)",
            (subagent_id, role, message, timestamp)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def get_subagent_messages(subagent_id: str) -> List[Dict[str, Any]]:
    """Retrieves all coordination logs for a subagent."""
    conn = sqlite3.connect(DB_FILE_PATH)
    results = []
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, message, timestamp FROM subagent_messages WHERE subagent_id = ? ORDER BY timestamp ASC",
            (subagent_id,)
        )
        rows = cursor.fetchall()
        for r in rows:
            results.append({
                "role": r[0],
                "message": r[1],
                "timestamp": r[2]
            })
    except Exception:
        pass
    finally:
        conn.close()
    return results

def update_model_quotas(model_family: str, pct_5h: float, pct_weekly: float) -> None:
    """Updates or inserts the quota usage percentages for a model family."""
    conn = sqlite3.connect(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        last_updated = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            """
            INSERT OR REPLACE INTO model_quotas (model_family, pct_5h, pct_weekly, last_updated)
            VALUES (?, ?, ?, ?)
            """,
            (model_family, pct_5h, pct_weekly, last_updated)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def get_model_quotas() -> List[Dict[str, Any]]:
    """Retrieves all current model quota records."""
    conn = sqlite3.connect(DB_FILE_PATH)
    results = []
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT model_family, pct_5h, pct_weekly, last_updated FROM model_quotas")
        rows = cursor.fetchall()
        for r in rows:
            results.append({
                "model_family": r[0],
                "pct_5h": r[1],
                "pct_weekly": r[2],
                "last_updated": r[3]
            })
    except Exception:
        pass
    finally:
        conn.close()
    return results

def ensure_plugin_scheduled_task(name: str, prompt: str, cron_expr: str) -> None:
    """Helper for plugins to register a default scheduled task in the database."""
    import sqlite3
    import uuid
    from datetime import datetime, timezone
    
    # We resolve get_next_cron_run dynamically to avoid circular import with web.py
    try:
        from agent.web import get_next_cron_run
        next_run_dt = get_next_cron_run(cron_expr, datetime.now(timezone.utc))
        next_run = next_run_dt.isoformat()
    except Exception:
        from datetime import timedelta
        next_run = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

    conn = sqlite3.connect(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        # Check if a task with the same name already exists
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

# Initialize database on module load
try:
    init_db()
except Exception:
    pass
