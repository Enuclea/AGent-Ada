"""SQLite database setup, schema initialization, and shared constants.

This module owns the database connection monkeypatch, DB_FILE_PATH,
the full schema (init_db), and the active_session_id ContextVar.
"""

import json
import os
import sqlite3

# Globally monkeypatch sqlite3.connect to set 10s timeout and enable WAL mode for concurrency
_orig_sqlite3_connect = sqlite3.connect
def custom_sqlite3_connect(database, *args, **kwargs):
    kwargs.setdefault("timeout", 10.0)
    conn = _orig_sqlite3_connect(database, *args, **kwargs)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return conn
sqlite3.connect = custom_sqlite3_connect

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import contextvars

active_session_id_var = contextvars.ContextVar("active_session_id", default=None)

DB_FILE_PATH = Path(os.getenv("AGENT_DB_PATH", str(Path.home() / ".agent" / "history.db")))

def init_db() -> None:
    """Initializes the SQLite conversation history and FTS5 search virtual tables."""
    DB_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        # Persistent memory key-values
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS persistent_memory (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)
        # Telemetry logs
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            event_type TEXT,
            event_details TEXT,
            latency REAL,
            timestamp TEXT
        )
        """)
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
        try:
            cursor.execute("ALTER TABLE session_plans ADD COLUMN goal TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE session_plans ADD COLUMN acceptance_criteria TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE session_plans ADD COLUMN non_goals TEXT")
        except sqlite3.OperationalError:
            pass
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
        # Task checkpoints (for resuming long-running tasks across sessions)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_checkpoints (
            id TEXT PRIMARY KEY,
            task_name TEXT NOT NULL,
            session_id TEXT,
            phase TEXT NOT NULL,
            step_completed INTEGER NOT NULL,
            total_steps INTEGER,
            state_json TEXT,
            status TEXT DEFAULT 'in_progress',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
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
        try:
            cursor.execute("ALTER TABLE subagent_messages ADD COLUMN parent_session_id TEXT")
        except sqlite3.OperationalError:
            pass
        # Model quotas
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS model_quotas (
            model_family TEXT PRIMARY KEY,
            pct_5h REAL,
            pct_weekly REAL,
            last_updated TEXT
        )
        """)
        # Remote workers
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            worker_id TEXT PRIMARY KEY,
            host TEXT,
            capabilities TEXT,
            platform TEXT,
            status TEXT DEFAULT 'online',
            active_tasks INTEGER DEFAULT 0,
            max_concurrent INTEGER DEFAULT 3,
            has_agy INTEGER DEFAULT 0,
            has_grok INTEGER DEFAULT 0,
            registered_at TEXT,
            last_heartbeat TEXT,
            metadata TEXT
        )
        """)
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

# Initialize database on module load
try:
    init_db()
except Exception:
    pass
