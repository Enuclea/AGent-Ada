import os
import tempfile
import sqlite3
import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

# Force a clean temporary database for hermes testing to protect the user's live database
tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp_db_path = tmp_db.name
tmp_db.close()
os.environ["AGENT_DB_PATH"] = tmp_db_path

from fastapi.testclient import TestClient
from agent.web import app
from agent import memory
from agent import tools

# Override DB_FILE_PATH at the canonical source since memory module might have been loaded earlier
import agent.db
agent.db.DB_FILE_PATH = Path(tmp_db_path)
client = TestClient(app)

def setup_module():
    # Ensure DB is initialized
    memory.init_db()

def teardown_module():
    try:
        os.remove(tmp_db_path)
    except OSError:
        pass

def test_plan_db_helpers():
    """Test session plan storage and status transition helper functions."""
    plan_id = "test-plan-1"
    sess_id = "test-session-1"
    
    # 1. Add plan
    memory.add_session_plan(plan_id, sess_id, "Test Plan Title")
    
    # 2. Add steps
    memory.add_plan_step("step-1", plan_id, 1, "First Step", "pending", "run_command", "echo 1")
    memory.add_plan_step("step-2", plan_id, 2, "Second Step", "pending")
    
    # 3. Retrieve and verify
    plan = memory.get_session_plan(sess_id)
    assert plan is not None
    assert plan["id"] == plan_id
    assert plan["title"] == "Test Plan Title"
    assert len(plan["steps"]) == 2
    assert plan["steps"][0]["id"] == "step-1"
    assert plan["steps"][0]["step_order"] == 1
    assert plan["steps"][0]["status"] == "pending"
    assert plan["steps"][0]["assigned_tool"] == "run_command"
    assert plan["steps"][0]["assigned_args"] == "echo 1"
    
    # 4. Update status and verify
    memory.update_plan_step_status("step-1", "running")
    plan = memory.get_session_plan(sess_id)
    assert plan["steps"][0]["status"] == "running"
    
    memory.update_plan_step_status("step-1", "completed")
    plan = memory.get_session_plan(sess_id)
    assert plan["steps"][0]["status"] == "completed"

def test_telemetry_db_helpers():
    """Test logging and retrieving token usage telemetry."""
    sess_id = "test-session-telemetry"
    memory.log_token_usage(sess_id, "gemini-3.5-flash", 100, 200, 0.0001)
    memory.log_token_usage(sess_id, "gemini-3.5-pro", 150, 250, 0.0002)
    
    telemetry = memory.get_token_usage_telemetry(sess_id)
    assert len(telemetry) == 2
    assert telemetry[0]["model_name"] == "gemini-3.5-flash"
    assert telemetry[0]["input_tokens"] == 100
    assert telemetry[0]["output_tokens"] == 200
    assert telemetry[0]["cost"] == 0.0001
    
    assert telemetry[1]["model_name"] == "gemini-3.5-pro"
    assert telemetry[1]["input_tokens"] == 150
    assert telemetry[1]["output_tokens"] == 250
    assert telemetry[1]["cost"] == 0.0002

def test_subagent_messages_db_helpers():
    """Test logging and retrieving subagent IPC messages."""
    sub_id = "subagent-123"
    memory.log_subagent_message(sub_id, "parent", "Start building feature")
    memory.log_subagent_message(sub_id, "child", "Feature complete")
    
    msgs = memory.get_subagent_messages(sub_id)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "parent"
    assert msgs[0]["message"] == "Start building feature"
    assert msgs[1]["role"] == "child"
    assert msgs[1]["message"] == "Feature complete"

def test_web_plan_and_telemetry_endpoints():
    """Test the web API endpoints for plans and telemetry."""
    sess_id = "test-sess-endpoints"
    plan_id = "plan-endpoints"
    
    # Seed DB
    memory.add_session_plan(plan_id, sess_id, "Endpoint Plan")
    memory.add_plan_step("step-ep", plan_id, 1, "Run verification")
    memory.log_token_usage(sess_id, "gemini-3.5-flash", 50, 50, 0.00005)
    
    # Test GET plan
    res_plan = client.get(f"/api/sessions/{sess_id}/plan")
    assert res_plan.status_code == 200
    plan_data = res_plan.json().get("plan")
    assert plan_data is not None
    assert plan_data["title"] == "Endpoint Plan"
    assert plan_data["steps"][0]["description"] == "Run verification"
    
    # Test GET telemetry
    res_tel = client.get(f"/api/sessions/{sess_id}/telemetry")
    assert res_tel.status_code == 200
    tel_data = res_tel.json().get("telemetry")
    assert len(tel_data) == 1
    assert tel_data[0]["model_name"] == "gemini-3.5-flash"
    assert tel_data[0]["input_tokens"] == 50

@pytest.mark.anyio
async def test_run_command_timeout():
    """Test that run_command properly times out and terminates hung commands."""
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        with pytest.raises(TimeoutError) as exc_info:
            await tools.run_command("sleep 100")
        assert "timed out after 60 seconds" in str(exc_info.value)

@patch("agent.web.get_or_create_agent")
def test_model_fallback_routing(mock_get_or_create_agent):
    """Test model fallback routing when a model encounters a 429 quota exception."""
    mock_agent_primary = AsyncMock()
    mock_agent_primary.chat.side_effect = Exception("429 rate limit exceeded or quota exhausted")
    
    mock_agent_fallback = AsyncMock()
    mock_agent_fallback.conversation_id = "test-fallback-session"
    mock_agent_fallback.model = "gemini-1.5-flash"
    
    async def mock_iter_empty():
        if False:
            yield
    mock_agent_fallback.chat.return_value = mock_agent_fallback
    mock_agent_fallback.thoughts = mock_iter_empty()
    async def mock_response_chunks(*args, **kwargs):
        yield "Success fallback content"
    mock_agent_fallback.__aiter__ = mock_response_chunks
    mock_agent_fallback.usage_metadata = MagicMock(prompt_token_count=10, candidates_token_count=20)
    
    def side_effect(model, session_id, system_instructions, disable_tools, roleplay, prompt=None, **kwargs):
        if "3.5" in model:
            return mock_agent_primary
        else:
            return mock_agent_fallback
            
    mock_get_or_create_agent.side_effect = side_effect
    
    response = client.post("/api/chat", json={
        "prompt": "Hello",
        "session_id": "test-fallback-session",
        "model": "gemini-3.5-flash"
    })
    
    assert response.status_code == 200
    chunks = response.text
    assert "Success fallback content" in chunks
    
    # Verify fallback model usage was logged to telemetry
    telemetry = memory.get_token_usage_telemetry("test-fallback-session")
    assert len(telemetry) > 0
    assert telemetry[0]["model_name"] == "gemini-1.5-flash"
