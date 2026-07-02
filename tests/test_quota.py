import os
import tempfile
import pytest
from pathlib import Path

# Force a clean temporary database for testing
tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp_db_path = tmp_db.name
tmp_db.close()
os.environ["AGENT_DB_PATH"] = tmp_db_path

from fastapi.testclient import TestClient
import sqlite3
from unittest.mock import AsyncMock, patch, MagicMock
from agent.web import app, fetch_real_quotas_sync
from agent import memory

# Override DB_FILE_PATH at the canonical source
import agent.db
agent.db.DB_FILE_PATH = Path(tmp_db_path)

client = TestClient(app)

def setup_module():
    memory.init_db()

def teardown_module():
    try:
        os.remove(tmp_db_path)
    except OSError:
        pass

def test_database_helpers():
    conn = sqlite3.connect(memory.DB_FILE_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM model_quotas")
    conn.commit()
    conn.close()

    memory.update_model_quotas("gemini", 90.0, 85.0)
    memory.update_model_quotas("claude_gpt", 100.0, 99.0)

    quotas = memory.get_model_quotas()
    assert len(quotas) == 2
    gemini = next(q for q in quotas if q["model_family"] == "gemini")
    assert gemini["pct_5h"] == 90.0
    assert gemini["pct_weekly"] == 85.0

    claude = next(q for q in quotas if q["model_family"] == "claude_gpt")
    assert claude["pct_5h"] == 100.0
    assert claude["pct_weekly"] == 99.0

@patch("agent.web.discover_language_server")
@patch("requests.post")
def test_fetch_real_quotas(mock_post, mock_discover):
    mock_discover.return_value = (12345, "token123", [45887])
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "response": {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"window": "5h", "remainingFraction": 0.95},
                        {"window": "weekly", "remainingFraction": 0.88}
                    ]
                },
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {"window": "5h", "remainingFraction": 1.0},
                        {"window": "weekly", "remainingFraction": 0.99}
                    ]
                }
            ]
        }
    }
    mock_post.return_value = mock_response

    res = fetch_real_quotas_sync()
    assert res is True

    quotas = memory.get_model_quotas()
    gemini = next(q for q in quotas if q["model_family"] == "gemini")
    assert gemini["pct_5h"] == 95.0
    assert gemini["pct_weekly"] == 88.0

def test_api_endpoint():
    response = client.get("/api/quotas")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 2

@patch("agent.web.get_or_create_agent")
def test_quota_failover_routing(mock_get_agent):
    # Set Gemini remaining quota to < 20.0 (high usage)
    memory.update_model_quotas("gemini", 15.0, 10.0)

    # Set up mock agent and response using generator methods directly
    mock_agent = AsyncMock()
    mock_agent.conversation_id = "test-session-failover"
    
    async def mock_thoughts_gen():
        if False:
            yield
    async def mock_response_chunks():
        yield "Response"
        
    mock_agent.chat.return_value = mock_agent
    mock_agent.thoughts = mock_thoughts_gen()
    mock_agent.__aiter__ = lambda s: mock_response_chunks()
    mock_agent.usage_metadata = MagicMock(prompt_token_count=10, candidates_token_count=20)
    
    mock_get_agent.return_value = mock_agent

    response = client.post("/api/chat", json={
        "prompt": "Test failover",
        "session_id": "test-session-failover"
    })
    
    assert response.status_code == 200
    
    # Verify that get_or_create_agent was called with Claude
    args, kwargs = mock_get_agent.call_args
    assert args[0] == "Claude Sonnet 4.6 (Thinking)"
    assert args[1] == "test-session-failover"

@patch("agent.web.get_or_create_agent")
def test_stuck_prevention_fallback(mock_get_agent):
    # Reset Gemini quota to 100% so we start with Gemini
    memory.update_model_quotas("gemini", 100.0, 100.0)

    # Make the first agent fail (simulate getting stuck/errored)
    mock_agent_gemini = AsyncMock()
    mock_agent_gemini.chat.side_effect = Exception("Connection timeout/stuck")

    # Make the fallback agent succeed
    mock_agent_claude = AsyncMock()
    mock_agent_claude.conversation_id = "test-session-fallback-success"
    mock_agent_claude.model = "Claude Sonnet 4.6 (Thinking)"
    
    async def mock_thoughts_gen():
        yield "Claude Thinking..."
    async def mock_response_chunks():
        yield "Claude Response"
        
    mock_agent_claude.chat.return_value = mock_agent_claude
    mock_agent_claude.thoughts = mock_thoughts_gen()
    mock_agent_claude.__aiter__ = lambda s: mock_response_chunks()
    mock_agent_claude.usage_metadata = MagicMock(prompt_token_count=10, candidates_token_count=20)

    # Configure mock_get_agent side_effect
    def side_effect(model, session_id, system_instructions=None, disable_tools=False, roleplay=False, prompt=None):
        if model and "Claude" in model:
            return mock_agent_claude
        return mock_agent_gemini

    mock_get_agent.side_effect = side_effect

    # Call api/chat
    response = client.post("/api/chat", json={
        "prompt": "Test stuck retry",
        "session_id": "test-session-stuck"
    })

    assert response.status_code == 200
    body = response.text
    assert "Claude Response" in body
    assert "Model got stuck/errored" in body
