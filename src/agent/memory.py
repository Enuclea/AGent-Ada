"""Backward-compatible re-export shim.

All functionality has been split into focused modules:
- agent.db           — SQLite setup, schema, DB_FILE_PATH
- agent.persistence  — Facts, KV storage, roleplay memories, compaction
- agent.conversation — Step logging, FTS5 search, RAG context
- agent.task_manager — Active tasks, plans, scheduling, approval bridge
- agent.telemetry    — Token usage, quotas, subagent messaging, workers

Existing code using `from agent import memory; memory.add_fact(...)` continues
to work unchanged. New code should import from the specific module.
"""

import agent.storage.db as _db

# --- Database layer ---
# DB_FILE_PATH and init_db are accessed via __getattr__ below for
# late-binding (so test patches on agent.db.DB_FILE_PATH propagate).
from agent.storage.db import (  # noqa: F401
    init_db,
    custom_sqlite3_connect,
    active_session_id_var,
)

# --- Persistence (facts, KV, roleplay) ---
from agent.storage.persistence import (  # noqa: F401
    MEMORY_FILE_PATH,
    get_memory_file,
    load_memory,
    save_memory,
    add_fact,
    update_key_value,
    get_fact_summary,
    add_roleplay_memory,
    get_roleplay_memories,
    compact_all_memories,
    active_roleplay_session_id,
    active_session_id,
)

# --- Conversation history & search ---
from agent.storage.conversation import (  # noqa: F401
    log_conversation_step,
    search_conversations,
    get_auto_rag_context,
)

# --- Task management ---
from agent.core.task_manager import (  # noqa: F401
    add_active_task,
    update_active_task_status,
    get_active_tasks,
    get_active_task_status,
    clear_active_tasks,
    add_task_log,
    get_task_logs,
    add_scheduled_task,
    get_scheduled_tasks,
    update_scheduled_task_run,
    delete_scheduled_task,
    ensure_plugin_scheduled_task,
    add_session_plan,
    add_plan_step,
    update_plan_step_status,
    get_session_plan,
    ask_discord_approval,
    save_checkpoint,
    get_checkpoint,
    complete_checkpoint,
    abandon_checkpoint,
    get_active_checkpoints,
    get_stale_checkpoints,
    auto_abandon_stale_checkpoints,
)

# --- Telemetry, quotas, subagents, workers ---
from agent.observability.telemetry import (  # noqa: F401
    log_token_usage,
    get_token_usage_telemetry,
    log_telemetry_event,
    log_subagent_message,
    get_subagent_messages,
    get_subagents_status,
    update_model_quotas,
    get_model_quotas,
    register_worker,
    get_registered_workers,
    update_worker_health,
    remove_worker,
)


def __getattr__(name):
    """Late-binding accessor for DB_FILE_PATH so test patches propagate."""
    if name == "DB_FILE_PATH":
        return _db.DB_FILE_PATH
    raise AttributeError(f"module 'agent.memory' has no attribute {name!r}")


from collections import OrderedDict
from typing import Any

class TieredLRUCache:
    def __init__(self, capacity: int = 5):
        self.capacity = capacity
        self.cache = OrderedDict()
        
    def clear(self) -> None:
        self.cache.clear()
        
    def get(self, key: str) -> Any:
        # 1. Check in-memory cache
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
            
        # 2. Check SQLite persistent_memory table
        import sqlite3
        import json
        from agent.storage.db import DB_FILE_PATH, init_db
        init_db()
        conn = sqlite3.connect(DB_FILE_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM persistent_memory WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                val = json.loads(row[0])
                # Put in cache, which might trigger eviction/overflow of another item
                self.set(key, val, persist_evicted=True)
                return val
        except Exception:
            pass
        finally:
            conn.close()
            
        return None
        
    def set(self, key: str, value: Any, persist_evicted: bool = True) -> None:
        if key in self.cache:
            self.cache[key] = value
            self.cache.move_to_end(key)
            return
            
        self.cache[key] = value
        if len(self.cache) > self.capacity:
            # Evict least recently used item
            evicted_key, evicted_val = self.cache.popitem(last=False)
            if persist_evicted:
                import sqlite3
                import json
                from agent.storage.db import DB_FILE_PATH, init_db
                init_db()
                conn = sqlite3.connect(DB_FILE_PATH)
                try:
                    cursor = conn.cursor()
                    val_str = json.dumps(evicted_val, ensure_ascii=False)
                    cursor.execute(
                        "INSERT OR REPLACE INTO persistent_memory (key, value) VALUES (?, ?)",
                        (evicted_key, val_str)
                    )
                    conn.commit()
                except Exception as e:
                    print(f"Warning: Failed to overflow cache to SQLite: {e}")
                finally:
                    conn.close()

global_cache = TieredLRUCache(capacity=5)
