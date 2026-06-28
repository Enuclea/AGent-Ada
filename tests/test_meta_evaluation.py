import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import tempfile
import os
import pytest
from unittest import mock
from datetime import datetime, timezone
import sqlite3
from agent import memory

# Force temporary database
@pytest.fixture
def temp_db_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "history.db"
        with mock.patch("agent.memory.DB_FILE_PATH", tmp_path), \
             mock.patch("enuclea.db.DEFAULT_DB_PATH", "/nonexistent/path.db"):
            memory.init_db()
            yield tmp_path

from pathlib import Path
from agent.meta_evaluation import run_meta_evaluation

@pytest.mark.anyio
async def test_run_meta_evaluation_no_data(temp_db_file):
    """Test run_meta_evaluation when there are no failed tasks or API errors."""
    with mock.patch("builtins.print") as mock_print:
        await run_meta_evaluation()
        mock_print.assert_any_call("[META-EVAL] No failed tasks or API errors in the last 24 hours.")

@pytest.mark.anyio
async def test_run_meta_evaluation_with_data(temp_db_file):
    """Test run_meta_evaluation with failed tasks and mock Gemini response."""
    # 1. Log a failed task to active_tasks
    task_id = "test-failed-task"
    memory.add_active_task(task_id, "Test Failed Task", "details about failure")
    memory.update_active_task_status(task_id, "failed")
    memory.add_task_log(task_id, "Something went wrong in tool execution")

    # 2. Mock KeylessAgyAgent response
    mock_response = mock.AsyncMock()
    async def mock_iter(*args, **kwargs):
        yield '["Handle division by zero when executing portfolio checks"]'
    mock_response.__aiter__ = mock_iter
    
    mock_agent_instance = mock.MagicMock()
    mock_agent_instance.chat = mock.AsyncMock(return_value=mock_response)

    with mock.patch("agent.meta_evaluation.KeylessAgyAgent", return_value=mock_agent_instance), \
         mock.patch("agent.memory.add_fact") as mock_add_fact:
         
        await run_meta_evaluation()
        
        # Verify KeylessAgyAgent chat was called with expected query
        mock_agent_instance.chat.assert_called_once()
        args, kwargs = mock_agent_instance.chat.call_args
        prompt_arg = args[0]
        assert "FAILED TASKS" in prompt_arg
        assert "Test Failed Task" in prompt_arg
        assert "Something went wrong in tool execution" in prompt_arg

        # Verify add_fact was called with the lesson learned
        mock_add_fact.assert_called_once_with("Handle division by zero when executing portfolio checks")


@pytest.mark.anyio
async def test_run_meta_evaluation_action_plan(temp_db_file):
    """Test run_meta_evaluation with a structured action_plan response."""
    # 1. Log a failed task
    task_id = "test-failed-task-plan"
    memory.add_active_task(task_id, "Test Failed Task with Action Plan", "details")
    memory.update_active_task_status(task_id, "failed")

    # 2. Mock JSON response with dictionary containing facts and action_plan
    json_response = """{
        "facts": ["Fact A", "Fact B"],
        "action_plan": [
            {
                "action_type": "improve_skill",
                "skill_name": "existing-skill",
                "description": "Updated desc",
                "instructions": "New updated instructions"
            },
            {
                "action_type": "create_skill",
                "skill_name": "new-skill-auto",
                "description": "Auto created skill description",
                "instructions": "Run command automatically",
                "script_content": "import sys\\nprint('hello')",
                "script_filename": "run.py"
            }
        ]
    }"""
    
    mock_response = mock.AsyncMock()
    async def mock_iter(*args, **kwargs):
        yield json_response
    mock_response.__aiter__ = mock_iter
    
    mock_agent_instance = mock.MagicMock()
    mock_agent_instance.chat = mock.AsyncMock(return_value=mock_response)

    with mock.patch("agent.meta_evaluation.KeylessAgyAgent", return_value=mock_agent_instance), \
         mock.patch("agent.memory.add_fact") as mock_add_fact, \
         mock.patch("agent.tools.improve_agent_skill") as mock_improve, \
         mock.patch("agent.tools.create_agent_skill") as mock_create:
         
        await run_meta_evaluation()
        
        # Verify facts were added
        mock_add_fact.assert_any_call("Fact A")
        mock_add_fact.assert_any_call("Fact B")
        assert mock_add_fact.call_count == 2

        # Verify improve_skill was called
        mock_improve.assert_called_once_with(
            skill_name="existing-skill",
            description="Updated desc",
            instructions="New updated instructions",
            script_content=None,
            script_filename=None
        )

        # Verify create_skill was called
        mock_create.assert_called_once_with(
            skill_name="new-skill-auto",
            description="Auto created skill description",
            instructions="Run command automatically",
            script_content="import sys\nprint('hello')",
            script_filename="run.py"
        )

