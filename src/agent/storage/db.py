"""SQLite database setup, schema initialization, and shared constants.

This module owns the database connection monkeypatch, DB_FILE_PATH,
the full schema (init_db), the active_session_id ContextVar, and
centralized helper functions for connection/query execution.
"""

import json
import os
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Generator, Union, Tuple
from contextlib import contextmanager
import contextvars

logger = logging.getLogger(__name__)

# Globally monkeypatch sqlite3.connect to set 10s timeout and enable WAL mode for concurrency
_orig_sqlite3_connect = sqlite3.connect
def custom_sqlite3_connect(database, *args, **kwargs):
    kwargs.setdefault("timeout", 10.0)
    conn = _orig_sqlite3_connect(database, *args, **kwargs)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
    return conn
sqlite3.connect = custom_sqlite3_connect

active_session_id_var = contextvars.ContextVar("active_session_id", default=None)
active_task_id_var = contextvars.ContextVar("active_task_id", default=None)

DB_FILE_PATH = Path(os.getenv("AGENT_DB_PATH", str(Path.home() / ".agent" / "history.db")))

def get_connection(db_path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    """
    Establishes and configures a SQLite connection with busy timeout and WAL mode enabled.
    
    Args:
        db_path: Path to the SQLite database file. If None, DB_FILE_PATH is used.
        
    Returns:
        sqlite3.Connection: A configured SQLite connection object.
    """
    if db_path is None:
        db_path = DB_FILE_PATH
        
    target_path = Path(db_path)
    
    # Ensure parent directory exists
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error("Failed to create database parent directory at %s: %s", target_path.parent, e)
        raise IOError(f"Could not prepare database directory path: {target_path.parent}") from e

    try:
        conn = sqlite3.connect(str(target_path), timeout=10.0)
        # Enable sqlite3.Row for dict-like access
        conn.row_factory = sqlite3.Row
        
        # Configure Write-Ahead Logging (WAL) and synchronous settings
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        
        return conn
    except sqlite3.Error as e:
        logger.error("SQLite connection/PRAGMA error for path %s: %s", target_path, e)
        raise

@contextmanager
def get_db_session(db_path: Optional[Union[str, Path]] = None) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager to safely acquire and release a database connection.
    Ensures that changes are committed on success, and rolled back on error.
    
    Args:
        db_path: Path to the database file.
        
    Yields:
        sqlite3.Connection: An open, transaction-managed SQLite connection.
    """
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception as e:
        logger.error("Database transaction failed, rolling back changes: %s", e)
        try:
            conn.rollback()
        except sqlite3.Error as rollback_err:
            logger.error("Failed to rollback transaction: %s", rollback_err)
        raise e
    finally:
        conn.close()

def execute_query(
    query: str, 
    params: Tuple[Any, ...] = (), 
    db_path: Optional[Union[str, Path]] = None
) -> List[sqlite3.Row]:
    """
    Executes a SELECT query securely using parameterized query formatting.
    
    Args:
        query: SQL statement. Must not contain string interpolation for parameters.
        params: Parameters to pass to the query execution sink.
        db_path: Database path override.
        
    Returns:
        List[sqlite3.Row]: Matching rows from the database.
    """
    with get_db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return cursor.fetchall()

def execute_write(
    query: str, 
    params: Tuple[Any, ...] = (), 
    db_path: Optional[Union[str, Path]] = None
) -> int:
    """
    Executes a write query (INSERT, UPDATE, DELETE) securely.
    
    Args:
        query: SQL statement. Must use parameterized query format.
        params: Parameters to bind to the SQL statement.
        db_path: Database path override.
        
    Returns:
        int: Number of affected rows.
    """
    with get_db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return cursor.rowcount

def init_db() -> None:
    """Initializes the SQLite conversation history and FTS5 search virtual tables."""
    DB_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(DB_FILE_PATH)
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
        # Route execution telemetry
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS route_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            route_name TEXT,
            model_name TEXT,
            status TEXT,
            error_message TEXT,
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
            reset_5h TEXT,
            reset_weekly TEXT,
            last_updated TEXT
        )
        """)
        try:
            cursor.execute("ALTER TABLE model_quotas ADD COLUMN reset_5h TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE model_quotas ADD COLUMN reset_weekly TEXT")
        except sqlite3.OperationalError:
            pass
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
        # LLM Route Caching
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS llm_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE,
            model TEXT,
            prompt TEXT,
            system_instructions TEXT,
            response TEXT,
            created_at INTEGER,
            ttl_seconds INTEGER,
            embedding TEXT
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
