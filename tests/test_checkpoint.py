"""Tests for the task checkpoint persistence layer (save, retrieve, complete, abandon, stale detection)."""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Redirect the DB to a temp file for each test."""
    from pathlib import Path
    db_file = tmp_path / "test_agent.db"
    monkeypatch.setattr("agent.db.DB_FILE_PATH", db_file)
    
    from agent import memory
    memory.init_db()
    yield str(db_file)


class TestCheckpointCRUD:
    """Tests for basic save, get, complete, abandon operations."""
    
    def test_save_and_retrieve_checkpoint(self):
        from agent.task_manager import save_checkpoint, get_checkpoint
        
        state = json.dumps({"topic_name": "gmail-push-notifications", "subscription_id": "sub-12345"})
        cp_id = save_checkpoint(
            task_name="setup_gmail_pubsub",
            session_id="session-abc",
            phase="topic_created",
            step_completed=1,
            state_json=state,
            total_steps=4
        )
        
        assert cp_id != ""
        
        result = get_checkpoint("setup_gmail_pubsub")
        assert result is not None
        assert result["task_name"] == "setup_gmail_pubsub"
        assert result["phase"] == "topic_created"
        assert result["step_completed"] == 1
        assert result["total_steps"] == 4
        assert result["status"] == "in_progress"
        assert json.loads(result["state_json"])["topic_name"] == "gmail-push-notifications"
    
    def test_checkpoint_upsert(self):
        """Updating an existing checkpoint should preserve the ID and update the state."""
        from agent.task_manager import save_checkpoint, get_checkpoint
        
        cp_id_1 = save_checkpoint(
            task_name="refactor_memory",
            session_id="session-1",
            phase="split_file",
            step_completed=1,
            state_json=json.dumps({"files_split": ["memory.py"]}),
            total_steps=5
        )
        
        cp_id_2 = save_checkpoint(
            task_name="refactor_memory",
            session_id="session-2",
            phase="migrate_tests",
            step_completed=3,
            state_json=json.dumps({"files_split": ["memory.py", "task_manager.py"], "tests_updated": True}),
            total_steps=5
        )
        
        # Should reuse the same checkpoint record
        assert cp_id_1 == cp_id_2
        
        result = get_checkpoint("refactor_memory")
        assert result["phase"] == "migrate_tests"
        assert result["step_completed"] == 3
        assert result["session_id"] == "session-2"
    
    def test_complete_checkpoint(self):
        from agent.task_manager import save_checkpoint, get_checkpoint, complete_checkpoint
        
        save_checkpoint(
            task_name="deploy_service",
            session_id="session-x",
            phase="docker_built",
            step_completed=2,
            state_json="{}",
            total_steps=3
        )
        
        result = complete_checkpoint("deploy_service")
        assert result is True
        
        # After completion, get_checkpoint should return None (only returns in_progress)
        assert get_checkpoint("deploy_service") is None
    
    def test_complete_nonexistent_checkpoint(self):
        from agent.task_manager import complete_checkpoint
        
        result = complete_checkpoint("nonexistent_task")
        assert result is False
    
    def test_abandon_checkpoint(self):
        from agent.task_manager import save_checkpoint, get_checkpoint, abandon_checkpoint
        
        save_checkpoint(
            task_name="old_task",
            session_id="session-old",
            phase="started",
            step_completed=1,
            state_json="{}",
        )
        
        result = abandon_checkpoint("old_task")
        assert result is True
        
        # After abandonment, get_checkpoint should return None
        assert get_checkpoint("old_task") is None
    
    def test_get_checkpoint_returns_none_for_missing(self):
        from agent.task_manager import get_checkpoint
        
        assert get_checkpoint("does_not_exist") is None


class TestActiveAndStaleCheckpoints:
    """Tests for listing active and detecting stale checkpoints."""
    
    def test_get_active_checkpoints(self):
        from agent.task_manager import save_checkpoint, get_active_checkpoints
        
        save_checkpoint("task_a", "s1", "phase_a", 1, "{}", 3)
        save_checkpoint("task_b", "s2", "phase_b", 2, "{}", 5)
        
        active = get_active_checkpoints()
        assert len(active) == 2
        task_names = {cp["task_name"] for cp in active}
        assert task_names == {"task_a", "task_b"}
    
    def test_stale_checkpoint_detection(self, tmp_db):
        from agent.task_manager import save_checkpoint, get_stale_checkpoints
        
        # Save a checkpoint, then manually backdate it
        save_checkpoint("stale_task", "s1", "old_phase", 1, "{}", 3)
        
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        conn = sqlite3.connect(tmp_db)
        conn.execute("UPDATE task_checkpoints SET updated_at = ? WHERE task_name = 'stale_task'", (old_time,))
        conn.commit()
        conn.close()
        
        stale = get_stale_checkpoints(max_age_hours=24)
        assert len(stale) == 1
        assert stale[0]["task_name"] == "stale_task"
    
    def test_auto_abandon_stale(self, tmp_db):
        from agent.task_manager import save_checkpoint, auto_abandon_stale_checkpoints, get_checkpoint
        
        save_checkpoint("stale_task_2", "s1", "old", 1, "{}", 3)
        
        # Backdate
        old_time = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        conn = sqlite3.connect(tmp_db)
        conn.execute("UPDATE task_checkpoints SET updated_at = ? WHERE task_name = 'stale_task_2'", (old_time,))
        conn.commit()
        conn.close()
        
        count = auto_abandon_stale_checkpoints(max_age_hours=24)
        assert count == 1
        
        # Should no longer be retrievable as active
        assert get_checkpoint("stale_task_2") is None
    
    def test_fresh_checkpoints_not_stale(self):
        from agent.task_manager import save_checkpoint, get_stale_checkpoints
        
        save_checkpoint("fresh_task", "s1", "recent", 1, "{}", 3)
        
        stale = get_stale_checkpoints(max_age_hours=24)
        assert len(stale) == 0


class TestCheckpointTools:
    """Tests for the agent-facing tool functions."""
    
    def test_checkpoint_task_save(self):
        from agent.tools import checkpoint_task
        
        result = json.loads(checkpoint_task(
            task_name="test_tool_task",
            phase="step_one_done",
            step_completed=1,
            state=json.dumps({"key": "value"}),
            total_steps=3
        ))
        
        assert result["status"] == "saved"
        assert result["task_name"] == "test_tool_task"
        assert result["checkpoint_id"] != ""
    
    def test_checkpoint_task_complete(self):
        from agent.tools import checkpoint_task
        
        # First save a checkpoint
        checkpoint_task("completion_test", "in_progress", 1, "{}", 2)
        
        # Then mark as complete
        result = json.loads(checkpoint_task("completion_test", "completed", 2, "{}"))
        assert result["status"] == "completed"
    
    def test_checkpoint_task_invalid_json_state(self):
        """Non-JSON state should be wrapped automatically."""
        from agent.tools import checkpoint_task
        
        result = json.loads(checkpoint_task(
            task_name="invalid_json_test",
            phase="started",
            step_completed=1,
            state="this is not json",
            total_steps=2
        ))
        
        assert result["status"] == "saved"
    
    def test_get_task_checkpoint_found(self):
        from agent.tools import checkpoint_task, get_task_checkpoint
        
        checkpoint_task("lookup_test", "phase_2", 2, json.dumps({"data": 42}), 5)
        
        result = json.loads(get_task_checkpoint("lookup_test"))
        assert result["status"] == "found"
        assert result["step_completed"] == 2
        assert result["phase"] == "phase_2"
        assert json.loads(result["state"])["data"] == 42
    
    def test_get_task_checkpoint_not_found(self):
        from agent.tools import get_task_checkpoint
        
        result = json.loads(get_task_checkpoint("nonexistent"))
        assert result["status"] == "none"
        assert "No resumable checkpoint" in result["message"]


class TestResumeInjection:
    """Tests that the orchestrator correctly injects checkpoint context."""
    
    def test_orchestrator_checkpoint_injection(self):
        from agent.task_manager import save_checkpoint, get_active_checkpoints
        
        save_checkpoint("orchestrator_test", "s1", "building", 3, json.dumps({"built": True}), 6)
        
        active = get_active_checkpoints()
        assert len(active) >= 1
        
        # Simulate what the orchestrator does
        cp_context = "\n\n[RESUMABLE TASK CHECKPOINTS]\n"
        for cp in active:
            cp_context += (
                f"- Task: {cp['task_name']} | Phase: {cp['phase']} | "
                f"Step {cp['step_completed']}/{cp['total_steps'] or '?'} completed\n"
            )
        cp_context += "[END RESUMABLE TASK CHECKPOINTS]"
        
        assert "orchestrator_test" in cp_context
        assert "building" in cp_context
        assert "3/6" in cp_context
    
    def test_tool_registry_includes_checkpoint_tools(self):
        """Verify checkpoint tools are in the registered builtins."""
        from agent.registry import tool_registry
        from agent import tools
        
        registered = tool_registry.get_registered_tools(is_discord=False, disable_tools=False)
        tool_funcs = [t.__name__ if hasattr(t, '__name__') else str(t) for t in registered]
        
        assert "checkpoint_task" in tool_funcs
        assert "get_task_checkpoint" in tool_funcs
