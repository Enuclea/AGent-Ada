"""Persistent facts, key-value storage, roleplay memories, and legacy migration.

Extracted from memory.py — covers the JSON-based memory layer and
roleplay memory management.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set

import agent.storage.db as _db

MEMORY_FILE_PATH: Path = Path.home() / ".agent" / "memory.json"

def get_memory_file() -> Path:
    """Returns the path to the memory file and ensures its parent directory exists.

    Returns:
        Path: Path to the JSON memory file.
    """
    MEMORY_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return MEMORY_FILE_PATH

def load_memory() -> Dict[str, Any]:
    """Loads persistent memory from the SQLite database, with automatic migration from memory.json.

    Returns:
        Dict[str, Any]: Loaded dictionary containing 'facts' and 'key_value' structures.
    """
    _db.init_db()
    
    # 1. Try to read from SQLite
    data: Dict[str, Any] = {"facts": [], "key_value": {}}
    conn = _db.get_connection(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM persistent_memory WHERE key = ?", ("global_memory",))
        row = cursor.fetchone()
        if row:
            try:
                loaded = json.loads(row[0])
                if isinstance(loaded, dict):
                    data = loaded
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    finally:
        conn.close()

    # 2. Check for migration from legacy memory.json
    legacy_path = get_memory_file()
    if legacy_path.exists():
        try:
            with open(legacy_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
            # Save migrated memory to SQLite
            save_memory(data)
            # Safe deletion of legacy file
            try:
                legacy_path.unlink()
            except Exception:
                pass
        except Exception:
            pass
            
    data.setdefault("facts", [])
    data.setdefault("key_value", {})
    
    # 3. Merge individual key-values from SQLite (excluding special/structural keys)
    conn = _db.get_connection(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM persistent_memory")
        for key, val_str in cursor.fetchall():
            if key not in ("global_memory", "last_compaction", "session_mappings", "discord_config", "discord_members", "contacts"):
                try:
                    data["key_value"][key] = json.loads(val_str)
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        conn.close()
        
    # 4. Merge active in-memory cache items (these are the most recent)
    try:
        from agent.memory import global_cache
        for k, v in global_cache.cache.items():
            data["key_value"][k] = v
    except (ImportError, AttributeError):
        pass
        
    return data

def save_memory(memory_dict: Dict[str, Any]) -> None:
    """Saves the memory state to SQLite persistent_memory.

    Args:
        memory_dict: The memory dictionary structure to save.
    """
    _db.init_db()
    conn = _db.get_connection(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        val_str = json.dumps(memory_dict, ensure_ascii=False)
        cursor.execute(
            "INSERT OR REPLACE INTO persistent_memory (key, value) VALUES (?, ?)",
            ("global_memory", val_str)
        )
        conn.commit()
    except Exception as e:
        print(f"Warning: Failed to save persistent memory: {e}")
    finally:
        conn.close()

def add_fact(fact: str) -> str:
    """Appends a new fact to the persistent facts list.

    Args:
        fact: The text fact string to append.

    Returns:
        str: Success or skip log message.
    """
    mem = load_memory()
    facts: List[str] = mem["facts"]
    if fact not in facts:
        facts.append(fact)
        save_memory(mem)
        return f"Successfully added fact to persistent memory: '{fact}'"
    return f"Fact already exists in persistent memory: '{fact}'"

def update_key_value(key: str, value: Any) -> str:
    """Updates or sets a key-value pair in persistent memory.

    Args:
        key: The configuration or state key name.
        value: The value associated with key.

    Returns:
        str: Success message.
    """
    try:
        from agent.memory import global_cache
        global_cache.set(key, value)
    except (ImportError, AttributeError):
        mem = load_memory()
        mem["key_value"][key] = value
        save_memory(mem)
    return f"Successfully set memory key '{key}' to '{value}'"

def get_fact_summary() -> str:
    """Generates a text summary of the persistent memory.
    
    This is formatted for injection into the agent's system instructions.

    Returns:
        str: Formatted markdown string containing persistent history logs.
    """
    mem = load_memory()
    facts: List[str] = mem.get("facts", [])
    kv: Dict[str, Any] = mem.get("key_value", {})
    
    if not facts and not kv:
        return ""
        
    lines: List[str] = ["\n[PERSISTENT MEMORY FROM PAST SESSIONS]"]
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


# --- Roleplay Memories ---

active_roleplay_session_id: Optional[str] = None
active_session_id: Optional[str] = None

def add_roleplay_memory(session_id: str, key: str, fact: str) -> None:
    """Adds a new fact to the roleplay memory table.

    Args:
        session_id: The specific session/channel ID.
        key: Memory key identifier.
        fact: Fact content details.
    """
    conn = _db.get_connection(_db.DB_FILE_PATH)
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
    """Retrieves all roleplay memories saved for a specific session/channel, including shared rumors and messages.

    Args:
        session_id: Session ID to retrieve memories for.

    Returns:
        List[Dict[str, Any]]: List of matching rumor/fact objects.
    """
    conn = _db.get_connection(_db.DB_FILE_PATH)
    results: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()
    
    def add_row(k_val: str, f_val: str, ts_val: str) -> None:
        unique_key = (k_val.strip().lower(), f_val.strip().lower())
        if unique_key not in seen:
            seen.add(unique_key)
            results.append({
                "key": k_val,
                "fact": f_val,
                "timestamp": ts_val
            })

    try:
        cursor = conn.cursor()
        
        # Load local roleplay memories
        cursor.execute(
            "SELECT key, fact, timestamp FROM roleplay_memories WHERE session_id = ? ORDER BY id ASC",
            (session_id,)
        )
        for row in cursor.fetchall():
            add_row(row[0], row[1], row[2])
            
        # Retrieve system-wide rumores/past logs
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
            
        # Merge general main roleplay channel room rumors
        main_bar_session = "discord-roleplay-1518087367465111594"
        if session_id != main_bar_session:
            cursor.execute(
                "SELECT key, fact, timestamp FROM roleplay_memories WHERE session_id = ? ORDER BY id ASC",
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

    Returns:
        Dict[str, Any]: Compaction statistics detailing metrics before/after.
    """
    stats: Dict[str, Any] = {
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
    
    mem = load_memory()
    facts: List[str] = mem.get("facts", [])
    stats["memory_json_before_facts"] = len(facts)
    
    unique_facts: List[str] = []
    seen_normalized: Set[str] = set()
    for f in facts:
        norm = f.strip().lower()
        if not norm:
            continue
        if norm not in seen_normalized:
            seen_normalized.add(norm)
            unique_facts.append(f)
            
    final_facts: List[str] = []
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
    
    db_path = _db.DB_FILE_PATH
    if db_path.exists():
        stats["db_size_before"] = db_path.stat().st_size
        
        conn = _db.get_connection(db_path)
        try:
            cursor = conn.cursor()
            
            cursor.execute("SELECT count(*) FROM roleplay_memories")
            stats["roleplay_memories_before"] = cursor.fetchone()[0]
            
            cursor.execute("SELECT count(*) FROM active_tasks")
            stats["active_tasks_before"] = cursor.fetchone()[0]
            
            # Keep only the last 100 finished/completed active tasks to avoid bloat
            cursor.execute("""
                SELECT id FROM active_tasks 
                WHERE status IN ('completed', 'failed') 
                ORDER BY started_at DESC LIMIT 100
            """)
            keep_ids = [row[0] for row in cursor.fetchall()]
            
            import re
            pattern = re.compile(r"^[a-zA-Z0-9_\-\./]+$")
            keep_ids = [kid for kid in keep_ids if kid and pattern.match(str(kid))]
            
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
            
            # Orphan task logs deletion
            cursor.execute("DELETE FROM task_logs WHERE task_id NOT IN (SELECT id FROM active_tasks)")
            stats["task_logs_deleted"] = cursor.rowcount
            
            cursor.execute("SELECT id, session_id, key, fact, timestamp FROM roleplay_memories ORDER BY id ASC")
            all_rp = cursor.fetchall()
            
            main_bar_session = "discord-roleplay-1518087367465111594"
            main_bar_facts: Dict[Tuple[str, str], int] = {}
            other_facts: List[Tuple[int, str, str, str]] = []
            
            for row_id, sess_id, key, fact, ts in all_rp:
                norm_key = key.strip().lower()
                norm_fact = fact.strip().lower()
                if sess_id == main_bar_session:
                    main_bar_facts[(norm_key, norm_fact)] = row_id
                else:
                    other_facts.append((row_id, sess_id, norm_key, norm_fact))
                    
            ids_to_delete: Set[int] = set()
            for row_id, sess_id, norm_key, norm_fact in other_facts:
                if (norm_key, norm_fact) in main_bar_facts:
                    ids_to_delete.add(row_id)
                    
            seen_session_key_fact: Set[Tuple[str, str, str]] = set()
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
                ids_list = list(ids_to_delete)
                chunk_size = 999
                for i in range(0, len(ids_list), chunk_size):
                    chunk = ids_list[i : i + chunk_size]
                    placeholders = ",".join("?" for _ in chunk)
                    cursor.execute(f"DELETE FROM roleplay_memories WHERE id IN ({placeholders})", chunk)
                
            cursor.execute("SELECT count(*) FROM roleplay_memories")
            stats["roleplay_memories_after"] = cursor.fetchone()[0]
            
            conn.commit()
            cursor.execute("VACUUM")
        except Exception as e:
            print(f"Error during SQLite compaction: {e}")
        finally:
            conn.close()
            
        stats["db_size_after"] = db_path.stat().st_size
        
    try:
        update_key_value("last_compaction", datetime.now(timezone.utc).isoformat())
    except Exception:
        pass
        
    return stats
