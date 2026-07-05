"""Token usage telemetry, model quotas, subagent messaging, and remote worker registry.

Extracted from memory.py — covers all observability and infrastructure concerns.
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import agent.storage.db as _db


def log_token_usage(
    session_id: str,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    cost: float
) -> None:
    """Records token usage and cost calculation in the telemetry logs.

    Args:
        session_id: Active LLM chat session ID.
        model_name: Name of the model invoked.
        input_tokens: Count of context input prompt tokens.
        output_tokens: Count of generated output completion tokens.
        cost: Calculated API transaction cost.
    """
    conn = _db.get_connection(_db.DB_FILE_PATH)
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
    """Retrieves token usage telemetry records for a session.

    Args:
        session_id: Target session ID.

    Returns:
        List[Dict[str, Any]]: List of recorded telemetry step entries.
    """
    conn = _db.get_connection(_db.DB_FILE_PATH)
    results: List[Dict[str, Any]] = []
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

def log_telemetry_event(session_id: str, event_type: str, event_details: str, latency: float) -> None:
    """Logs a system/telemetry event to SQLite.

    Args:
        session_id: Current agent session context.
        event_type: Telemetry event type identifier.
        event_details: Descriptive event payloads.
        latency: Time taken in seconds.
    """
    _db.init_db()
    conn = _db.get_connection(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        timestamp = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "INSERT INTO telemetry_logs (session_id, event_type, event_details, latency, timestamp) VALUES (?, ?, ?, ?, ?)",
            (session_id, event_type, event_details, latency, timestamp)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


# --- Subagent Messaging ---

def log_subagent_message(
    subagent_id: str,
    role: str,
    message: str,
    parent_session_id: Optional[str] = None
) -> None:
    """Records a messaging communication log between parent and subagent.

    Args:
        subagent_id: Subagent's unique conversation ID.
        role: Message author role (parent, subagent).
        message: Content of the message.
        parent_session_id: Parent's chat session ID.
    """
    conn = _db.get_connection(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        timestamp = datetime.now(timezone.utc).isoformat()
        
        if not parent_session_id:
            import os
            parent_session_id = _db.active_session_id_var.get() or os.environ.get("ACTIVE_SESSION_ID")
            
        if parent_session_id:
            cursor.execute(
                "INSERT INTO subagent_messages (subagent_id, role, message, timestamp, parent_session_id) VALUES (?, ?, ?, ?, ?)",
                (subagent_id, role, message, timestamp, parent_session_id)
            )
            cursor.execute(
                "UPDATE subagent_messages SET parent_session_id = ? WHERE subagent_id = ? AND parent_session_id IS NULL",
                (parent_session_id, subagent_id)
            )
        else:
            cursor.execute("SELECT parent_session_id FROM subagent_messages WHERE subagent_id = ? AND parent_session_id IS NOT NULL LIMIT 1", (subagent_id,))
            row = cursor.fetchone()
            resolved_parent = row[0] if row else None
            cursor.execute(
                "INSERT INTO subagent_messages (subagent_id, role, message, timestamp, parent_session_id) VALUES (?, ?, ?, ?, ?)",
                (subagent_id, role, message, timestamp, resolved_parent)
            )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def get_subagent_messages(subagent_id: str) -> List[Dict[str, Any]]:
    """Retrieves all coordination logs for a subagent.

    Args:
        subagent_id: Unique subagent conversation identifier.

    Returns:
        List[Dict[str, Any]]: Historical exchange logs between parent and child.
    """
    conn = _db.get_connection(_db.DB_FILE_PATH)
    results: List[Dict[str, Any]] = []
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

def get_subagents_status() -> List[Dict[str, Any]]:
    """Retrieves status and metadata for all spawned subagents.

    Returns:
        List[Dict[str, Any]]: List of subagent tracking status objects.
    """
    conn = _db.get_connection(_db.DB_FILE_PATH)
    results: List[Dict[str, Any]] = []
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT subagent_id, role, message, timestamp, parent_session_id FROM subagent_messages ORDER BY timestamp ASC"
        )
        all_messages = cursor.fetchall()
        
        from collections import defaultdict
        grouped_messages = defaultdict(list)
        for subagent_id, role, message, timestamp, parent_session_id in all_messages:
            grouped_messages[subagent_id].append((role, message, timestamp, parent_session_id))
            
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
                else:
                    completed_at = None
                    
            results.append({
                "subagent_id": sid,
                "parent_session_id": parent_session_id,
                "prompt": prompt,
                "status": status,
                "started_at": started_at,
                "completed_at": completed_at
            })
        results.sort(key=lambda x: x["started_at"], reverse=True)
    except Exception:
        pass
    finally:
        conn.close()
    return results


# --- Model Quotas ---

def update_model_quotas(model_family: str, pct_5h: float, pct_weekly: float) -> None:
    """Updates or inserts the quota usage percentages for a model family.

    Args:
        model_family: Identifier for the model provider family.
        pct_5h: Percentage usage over the past 5 hours.
        pct_weekly: Percentage usage over the past week.
    """
    conn = _db.get_connection(_db.DB_FILE_PATH)
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
    """Retrieves all current model quota records.

    Returns:
        List[Dict[str, Any]]: List of quota tracking objects.
    """
    conn = _db.get_connection(_db.DB_FILE_PATH)
    results: List[Dict[str, Any]] = []
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


# --- Remote Worker Registry ---

def register_worker(
    worker_id: str,
    host: str,
    capabilities: List[str],
    platform_name: str = "",
    max_concurrent: int = 3,
    has_agy: bool = False,
    has_grok: bool = False,
    metadata: Optional[Dict[str, Any]] = None
) -> None:
    """Registers or updates a remote worker node.

    Args:
        worker_id: Unique worker node name.
        host: Host endpoint target.
        capabilities: List of capability tags.
        platform_name: Platform OS name.
        max_concurrent: Max concurrent task limit.
        has_agy: True if agy command is supported natively.
        has_grok: True if Grok/Magica APIs are accessible.
        metadata: Extra environment descriptors.
    """
    conn = _db.get_connection(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        caps_str = ",".join(capabilities) if isinstance(capabilities, list) else str(capabilities)
        meta_str = json.dumps(metadata) if metadata else "{}"
        cursor.execute(
            """
            INSERT INTO workers (worker_id, host, capabilities, platform, status,
                                active_tasks, max_concurrent, has_agy, has_grok,
                                registered_at, last_heartbeat, metadata)
            VALUES (?, ?, ?, ?, 'online', 0, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                host = excluded.host,
                capabilities = excluded.capabilities,
                platform = excluded.platform,
                status = 'online',
                max_concurrent = excluded.max_concurrent,
                has_agy = excluded.has_agy,
                has_grok = excluded.has_grok,
                last_heartbeat = excluded.last_heartbeat,
                metadata = excluded.metadata
            """,
            (worker_id, host, caps_str, platform_name, max_concurrent,
             int(has_agy), int(has_grok), now, now, meta_str)
        )
        conn.commit()
    except Exception as e:
        print(f"[MEMORY] Failed to register worker {worker_id}: {e}")
    finally:
        conn.close()


def get_registered_workers() -> List[Dict[str, Any]]:
    """Returns all registered workers.

    Returns:
        List[Dict[str, Any]]: List of registered worker nodes and metrics.
    """
    conn = _db.get_connection(_db.DB_FILE_PATH)
    results: List[Dict[str, Any]] = []
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT worker_id, host, capabilities, platform, status, active_tasks, "
            "max_concurrent, has_agy, has_grok, registered_at, last_heartbeat, metadata "
            "FROM workers ORDER BY last_heartbeat DESC"
        )
        for row in cursor.fetchall():
            caps = row[2].split(",") if row[2] else []
            meta = {}
            try:
                meta = json.loads(row[11]) if row[11] else {}
            except (json.JSONDecodeError, TypeError):
                pass
            results.append({
                "worker_id": row[0],
                "host": row[1],
                "capabilities": caps,
                "platform": row[3],
                "status": row[4],
                "active_tasks": row[5],
                "max_concurrent": row[6],
                "has_agy": bool(row[7]),
                "has_grok": bool(row[8]),
                "registered_at": row[9],
                "last_heartbeat": row[10],
                "metadata": meta,
            })
    except Exception:
        pass
    finally:
        conn.close()
    return results


def update_worker_health(
    worker_id: str,
    status: str = "online",
    active_tasks: Optional[int] = None
) -> None:
    """Updates a worker's health status and optional task count.

    Args:
        worker_id: Unique worker identifier.
        status: Current health state (online, offline).
        active_tasks: Active concurrent task execution count.
    """
    conn = _db.get_connection(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        if active_tasks is not None:
            cursor.execute(
                "UPDATE workers SET status = ?, active_tasks = ?, last_heartbeat = ? WHERE worker_id = ?",
                (status, active_tasks, now, worker_id)
            )
        else:
            cursor.execute(
                "UPDATE workers SET status = ?, last_heartbeat = ? WHERE worker_id = ?",
                (status, now, worker_id)
            )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def remove_worker(worker_id: str) -> None:
    """Removes a worker node registration.

    Args:
        worker_id: Worker name target.
    """
    conn = _db.get_connection(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM workers WHERE worker_id = ?", (worker_id,))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
