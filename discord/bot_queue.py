import sqlite3
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

import os

# Store discord queue database in writable /data folder if available
db_dir = os.environ.get("AGENT_DB_PATH")
if db_dir:
    DB_PATH = Path(db_dir).parent / "discord_queue.db"
else:
    DB_PATH = Path(__file__).parent / "discord_queue.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS discord_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            priority INTEGER,
            task_type TEXT,
            channel_id INTEGER,
            message_id INTEGER,
            prompt_text TEXT,
            placeholder_id INTEGER,
            timestamp REAL,
            status TEXT
        )
        """)
        conn.commit()
    finally:
        conn.close()

def add_task(priority: int, task_type: str, channel_id: int, message_id: int, prompt_text: Optional[str] = None) -> int:
    try:
        channel_id = int(channel_id)
    except (ValueError, TypeError):
        channel_id = 0
    try:
        message_id = int(message_id)
    except (ValueError, TypeError):
        message_id = 0
        
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO discord_tasks (priority, task_type, channel_id, message_id, prompt_text, timestamp, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """, (priority, task_type, channel_id, message_id, prompt_text, time.time()))
        task_id = cursor.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()

def update_task_placeholder(task_id: int, placeholder_id: int):
    try:
        placeholder_id = int(placeholder_id)
    except (ValueError, TypeError):
        placeholder_id = 0
        
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("""
        UPDATE discord_tasks SET placeholder_id = ? WHERE id = ?
        """, (placeholder_id, task_id))
        conn.commit()
    finally:
        conn.close()

def update_task_status(task_id: int, status: str):
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("""
        UPDATE discord_tasks SET status = ? WHERE id = ?
        """, (status, task_id))
        conn.commit()
    finally:
        conn.close()

def get_pending_tasks() -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
        SELECT * FROM discord_tasks 
        WHERE status IN ('pending', 'processing') 
        ORDER BY priority ASC, timestamp ASC
        """)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
