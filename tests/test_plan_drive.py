import os
import tempfile
import pytest
import json
from pathlib import Path
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock

# Force a clean temporary database for web client testing
tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp_db_path = tmp_db.name
tmp_db.close()

from agent import memory
# Override DB_FILE_PATH directly
memory.DB_FILE_PATH = Path(tmp_db_path)
memory.init_db()

from agent.web import app

def setup_module():
    pass

def teardown_module():
    try:
        os.remove(tmp_db_path)
    except OSError:
        pass


def test_plan_then_drive_flow():
    """Test that plan-then-drive executes sequential steps and updates status correctly."""
    # 1. Mock the planner response (decomposing into two steps)
    mock_planner_response = MagicMock()
    mock_planner_response.text = '[{"description": "First Step", "assigned_tool": "run_command"}, {"description": "Second Step", "assigned_tool": "view_file"}]'
        
    mock_planner_agent = MagicMock()
    mock_planner_agent.__aenter__ = AsyncMock(return_value=mock_planner_agent)
    mock_planner_agent.__aexit__ = AsyncMock()
    mock_planner_agent.chat = AsyncMock(return_value=mock_planner_response)
    
    # 2. Mock the active execution agent
    async def mock_chat_generator(prompt):
        # We simulate thoughts and chunks
        class MockResponse:
            def __init__(self, thoughts_list, chunk_list):
                self.thoughts = self._thoughts_gen(thoughts_list)
                self.chunks = chunk_list
                self.usage_metadata = MagicMock(prompt_token_count=10, candidates_token_count=20)
                
            async def _thoughts_gen(self, lst):
                for t in lst:
                    yield t
                    
            def __aiter__(self):
                return self._chunks_gen()
                
            async def _chunks_gen(self):
                for c in self.chunks:
                    yield c
                    
        if "First Step" in prompt:
            return MockResponse(["Thinking step 1..."], ["Result from step 1"])
        else:
            return MockResponse(["Thinking step 2..."], ["Result from step 2"])

    mock_exec_agent = MagicMock()
    mock_exec_agent.conversation_id = "test-drive-session-123"
    mock_exec_agent.model = "gemini-3.5-flash"
    mock_exec_agent.chat = AsyncMock(side_effect=mock_chat_generator)
    
    # We patch the agent instantiator
    with patch.dict(os.environ, {"AGENT_ENABLE_PLAN_DECOMPOSITION": "true"}), \
         patch("agent.keyless.KeylessAgyAgent", return_value=mock_planner_agent), \
         patch("agent.web.get_or_create_agent", return_value=mock_exec_agent), \
         patch("agent.web.load_plugins"):  # Mock plugins to avoid extra logs/warnings
         
         with TestClient(app) as client:
             payload = {
                  "prompt": "Create a file called test_output.txt in the project root with the content 'Hello World', then read the file back and verify the content is correct. This is a multi-step operation requiring file creation, writing, and validation of the output.",
                 "session_id": "test-drive-session-123"
             }
             response = client.post("/api/chat", json=payload)
             assert response.status_code == 200
             
             # Verify response stream contains step progression outputs
             lines = response.text.split("\n")
             chunks = []
             for line in lines:
                 if line.startswith("data: "):
                     data_str = line[6:]
                     if data_str != "[DONE]":
                         try:
                             chunks.append(json.loads(data_str))
                         except json.JSONDecodeError:
                             pass
             
             # Assert that thoughts/chunks from both steps are present
             assert any("Step 1/2" in c.get("content", "") for c in chunks if c.get("type") == "thought")
             assert any("Result from step 1" in c.get("content", "") for c in chunks if c.get("type") == "chunk")
             assert any("Step 2/2" in c.get("content", "") for c in chunks if c.get("type") == "thought")
             assert any("Result from step 2" in c.get("content", "") for c in chunks if c.get("type") == "chunk")
             
             # Verify database states
             plan = memory.get_session_plan("test-drive-session-123")
             assert plan is not None
             assert len(plan["steps"]) == 2
             # Both steps should now be completed
             assert all(s["status"] == "completed" for s in plan["steps"])
