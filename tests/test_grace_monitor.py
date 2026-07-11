import os
import sqlite3
import pytest
import time
from unittest.mock import patch, MagicMock
from pathlib import Path
from datetime import datetime, timezone, timedelta
import json

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import agent.grace_monitor as grace_monitor

def test_grace_monitor_cleanup(tmp_path):
    # Set up a mock database path for grace_monitor
    db_file = tmp_path / "test_history.db"
    discord_db_file = tmp_path / "discord_queue.db"
    
    # Patch the DB_PATH in grace_monitor to point to our temp db
    with patch("agent.grace_monitor.DB_PATH", db_file), \
         patch("agent.grace_monitor.send_discord_alert") as mock_alert:
         
        # Create schema in the temp DB
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE active_tasks (
                id TEXT PRIMARY KEY,
                name TEXT,
                details TEXT,
                started_at TEXT,
                status TEXT,
                completed_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE task_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                timestamp TEXT,
                message TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE subagent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subagent_id TEXT,
                role TEXT,
                message TEXT,
                timestamp TEXT,
                parent_session_id TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE session_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                title TEXT,
                status TEXT,
                created_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE plan_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER,
                step_order INTEGER,
                description TEXT,
                status TEXT,
                assigned_tool TEXT,
                assigned_args TEXT,
                error_message TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE conversation_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                timestamp TEXT,
                role TEXT,
                content TEXT,
                tool_name TEXT,
                tool_result TEXT
            )
        """)
        
        # Scenario 1: A task that has been running for 5 minutes (Not Stalled, threshold is 10 mins)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        recent_started = (now - timedelta(minutes=5)).isoformat()
        cursor.execute(
            "INSERT INTO active_tasks (id, name, details, started_at, status) VALUES (?, ?, ?, ?, ?)",
            ("task-1", "test_command", "{'cmd': 'run'}", recent_started, "running")
        )
        
        # Scenario 2: A task that has been running and inactive for 30 minutes (Stalled)
        stalled_started = (now - timedelta(minutes=30)).isoformat()
        cursor.execute(
            "INSERT INTO active_tasks (id, name, details, started_at, status) VALUES (?, ?, ?, ?, ?)",
            ("task-2", "stalled_command", "{'cmd': 'hang'}", stalled_started, "running")
        )
        
        # Scenario 3: A subagent spawned 40 minutes ago with no update (Stalled subagent)
        sub_started = (now - timedelta(minutes=40)).isoformat()
        cursor.execute(
            "INSERT INTO subagent_messages (subagent_id, role, message, timestamp) VALUES (?, ?, ?, ?)",
            ("sub-1", "parent", "Spawning subagent in sandbox with prompt: test subagent", sub_started)
        )
        
        # Scenario 4: A subagent spawned 5 minutes ago (Not stalled subagent)
        sub_recent = (now - timedelta(minutes=5)).isoformat()
        cursor.execute(
            "INSERT INTO subagent_messages (subagent_id, role, message, timestamp) VALUES (?, ?, ?, ?)",
            ("sub-2", "parent", "Spawning subagent in sandbox with prompt: recent subagent", sub_recent)
        )
        
        # Scenario 5: A subagent spawned 40 minutes ago that has finished successfully (Not Stalled)
        sub_finished = (now - timedelta(minutes=40)).isoformat()
        cursor.execute(
            "INSERT INTO subagent_messages (subagent_id, role, message, timestamp) VALUES (?, ?, ?, ?)",
            ("sub-3", "parent", "Spawning subagent in sandbox", sub_finished)
        )
        cursor.execute(
            "INSERT INTO subagent_messages (subagent_id, role, message, timestamp) VALUES (?, ?, ?, ?)",
            ("sub-3", "subagent", "[SUCCESS] Boardroom contribution from docs_expert: {approved: true}", (now - timedelta(minutes=39)).isoformat())
        )
        
        # Scenario 6: A subagent spawned 40 minutes ago that has explicitly failed (Not Stalled)
        cursor.execute(
            "INSERT INTO subagent_messages (subagent_id, role, message, timestamp) VALUES (?, ?, ?, ?)",
            ("sub-4", "parent", "Spawning subagent in sandbox", sub_finished)
        )
        cursor.execute(
            "INSERT INTO subagent_messages (subagent_id, role, message, timestamp) VALUES (?, ?, ?, ?)",
            ("sub-4", "subagent", "[FAILED] Failed to parse boardroom contribution JSON: error", (now - timedelta(minutes=39)).isoformat())
        )
        
        # Scenario 7: A session plan running with no conversation steps for 30 minutes (Stalled Plan)
        plan_created = (now - timedelta(minutes=30)).isoformat()
        cursor.execute(
            "INSERT INTO session_plans (session_id, title, status, created_at) VALUES (?, ?, ?, ?)",
            ("session-plan-1", "Test Plan", "running", plan_created)
        )
        plan_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO plan_steps (plan_id, step_order, description, status) VALUES (?, ?, ?, ?)",
            (plan_id, 1, "First Step", "in_progress")
        )
        # Add conversation step 30 mins ago
        cursor.execute(
            "INSERT INTO conversation_steps (session_id, timestamp, role, content) VALUES (?, ?, ?, ?)",
            ("session-plan-1", (now - timedelta(minutes=30)).isoformat(), "user", "run plan")
        )

        # Scenario 8: A session plan running and active 5 mins ago (Not Stalled Plan)
        cursor.execute(
            "INSERT INTO session_plans (session_id, title, status, created_at) VALUES (?, ?, ?, ?)",
            ("session-plan-2", "Active Plan", "running", plan_created)
        )
        active_plan_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO plan_steps (plan_id, step_order, description, status) VALUES (?, ?, ?, ?)",
            (active_plan_id, 1, "First Step", "in_progress")
        )
        cursor.execute(
            "INSERT INTO conversation_steps (session_id, timestamp, role, content) VALUES (?, ?, ?, ?)",
            ("session-plan-2", (now - timedelta(minutes=5)).isoformat(), "user", "active step")
        )
        
        conn.commit()
        conn.close()

        # Set up a mock discord queue DB
        d_conn = sqlite3.connect(discord_db_file)
        d_cursor = d_conn.cursor()
        d_cursor.execute("""
            CREATE TABLE discord_tasks (
                id TEXT PRIMARY KEY,
                prompt_text TEXT,
                timestamp REAL,
                status TEXT
            )
        """)
        
        # Scenario 9: A discord task processing for 30 minutes (Stalled Discord Task)
        d_stalled_time = time.time() - (30 * 60)
        d_cursor.execute(
            "INSERT INTO discord_tasks (id, prompt_text, timestamp, status) VALUES (?, ?, ?, ?)",
            ("discord-task-1", "stalled discord prompt", d_stalled_time, "processing")
        )
        
        # Scenario 10: A discord task processing for 5 minutes (Not Stalled Discord Task)
        d_recent_time = time.time() - (5 * 60)
        d_cursor.execute(
            "INSERT INTO discord_tasks (id, prompt_text, timestamp, status) VALUES (?, ?, ?, ?)",
            ("discord-task-2", "recent discord prompt", d_recent_time, "processing")
        )
        
        d_conn.commit()
        d_conn.close()
        
        # Run check_tasks with 10 minutes threshold
        grace_monitor.check_tasks(inactivity_threshold_mins=10)
        
        # Verify DB changes
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        
        # Check task statuses
        cursor.execute("SELECT id, status FROM active_tasks")
        tasks = dict(cursor.fetchall())
        assert tasks["task-1"] == "running"
        assert tasks["task-2"] == "failed"  # Stalled task was auto-terminated
        
        # Check subagent statuses (determined by checking if failure msg was inserted)
        cursor.execute("SELECT message FROM subagent_messages WHERE subagent_id = 'sub-1' AND role = 'subagent'")
        sub1_msgs = cursor.fetchall()
        assert len(sub1_msgs) == 1
        assert "Terminated automatically by Grace Monitor" in sub1_msgs[0][0]
        
        cursor.execute("SELECT message FROM subagent_messages WHERE subagent_id = 'sub-2' AND role = 'subagent'")
        sub2_msgs = cursor.fetchall()
        assert len(sub2_msgs) == 0  # Not stalled, so no termination msg
        
        # Check that completed/failed subagents were not auto-terminated
        cursor.execute("SELECT message FROM subagent_messages WHERE subagent_id = 'sub-3' AND role = 'subagent'")
        sub3_msgs = cursor.fetchall()
        assert len(sub3_msgs) == 1  # No extra auto-termination message
        assert "[SUCCESS]" in sub3_msgs[0][0]
        
        cursor.execute("SELECT message FROM subagent_messages WHERE subagent_id = 'sub-4' AND role = 'subagent'")
        sub4_msgs = cursor.fetchall()
        assert len(sub4_msgs) == 1  # No extra auto-termination message
        assert "[FAILED]" in sub4_msgs[0][0]

        # Check plan statuses
        cursor.execute("SELECT id, status FROM session_plans")
        plans = dict(cursor.fetchall())
        assert plans[plan_id] == "failed"
        assert plans[active_plan_id] == "running"

        # Check plan steps statuses
        cursor.execute("SELECT plan_id, status, error_message FROM plan_steps")
        steps = cursor.fetchall()
        for p_id, status, err in steps:
            if p_id == plan_id:
                assert status == "failed"
                assert "Terminated automatically by Grace Monitor" in err
            elif p_id == active_plan_id:
                assert status == "in_progress"
        
        conn.close()

        # Check Discord tasks
        d_conn = sqlite3.connect(discord_db_file)
        d_cursor = d_conn.cursor()
        d_cursor.execute("SELECT id, status FROM discord_tasks")
        d_tasks = dict(d_cursor.fetchall())
        assert d_tasks["discord-task-1"] == "failed"
        assert d_tasks["discord-task-2"] == "processing"
        d_conn.close()
        
        # Verify alert was sent
        mock_alert.assert_called_once()
        assert "Auto-Cleaned Stalled Tasks" in mock_alert.call_args[0][0]
