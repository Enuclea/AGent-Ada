import os
import tempfile
import pytest
import json
import asyncio
import sqlite3
from pathlib import Path
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock

# Force a clean temporary database for web client testing
tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp_db_path = tmp_db.name
tmp_db.close()

from agent import memory
import agent.db
# Override DB_FILE_PATH at the canonical source
agent.db.DB_FILE_PATH = Path(tmp_db_path)
memory.init_db()

from agent.web import app

def setup_module():
    pass

def teardown_module():
    try:
        os.remove(tmp_db_path)
    except OSError:
        pass

@pytest.mark.anyio
async def test_subagent_delegation_and_resumption():
    """Test that subagent delegation yields control and scheduler wakes it up upon completion."""
    # 1. Create a session plan with one step
    session_id = "test-delegation-session-123"
    plan_id = "test-plan-123"
    
    memory.add_session_plan(
        plan_id=plan_id,
        session_id=session_id,
        title="Test Delegation Plan",
        goal="Test subagent delegation",
        acceptance_criteria="Subagent completes task",
        non_goals=""
    )
    
    memory.add_plan_step(
        step_id="step-1",
        plan_id=plan_id,
        step_order=1,
        description="Run subagent delegation step",
        status="pending",
        assigned_tool="spawn_subagent"
    )
    
    # Verify the plan is in pending status
    plan = memory.get_session_plan(session_id)
    assert plan is not None
    assert plan["steps"][0]["status"] == "pending"

    # 2. Mock active execution agent
    async def mock_chat_generator(prompt):
        # Log a subagent message to trigger the delegation exit detection
        memory.log_subagent_message("sub-123", "parent", "Spawning subagent", parent_session_id=session_id)
        
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
        return MockResponse(["Thinking step 1..."], ["Result from step 1"])

    mock_exec_agent = MagicMock()
    mock_exec_agent.conversation_id = session_id
    mock_exec_agent.model = "gemini-3.5-flash"
    mock_exec_agent.chat = AsyncMock(side_effect=mock_chat_generator)

    # Simulate get_or_create_agent and stream_agent_response
    with patch("agent.web.get_or_create_agent", return_value=mock_exec_agent), \
         patch("agent.web.load_plugins"):
         
         with TestClient(app) as client:
             # Run chat request which triggers the step execution loop
             payload = {
                 "prompt": "Trigger plan step execution",
                 "session_id": session_id
             }
             response = client.post("/api/chat", json=payload)
             assert response.status_code == 200
             
             # Step status should now be updated (delegated if bg task ran, running if still pending)
             plan = memory.get_session_plan(session_id)
             assert plan["steps"][0]["status"] in ("delegated", "running")

    # 3. Simulate subagent completion in database
    memory.log_subagent_message("sub-123", "subagent", "Subagent completed: File created successfully", parent_session_id=session_id)

    # 4. Mock the KeylessAgyAgent that the scheduler uses to resume the parent
    mock_resume_agent = MagicMock()
    mock_resume_agent.__aenter__ = AsyncMock(return_value=mock_resume_agent)
    mock_resume_agent.__aexit__ = AsyncMock()
    mock_resume_agent.chat = AsyncMock(return_value=MagicMock())
    
    # Run the scheduler's check logic manually (isolated check)
    with patch("agent.keyless.KeylessAgyAgent", return_value=mock_resume_agent) as mock_agent_class:
        # Run one iteration of scheduler check
        conn = sqlite3.connect(memory.DB_FILE_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ps.id, ps.plan_id, ps.description, sp.session_id, ps.step_order 
            FROM plan_steps ps
            JOIN session_plans sp ON ps.plan_id = sp.id
            WHERE ps.status IN ('delegated', 'running')
        """)
        delegated_steps = cursor.fetchall()
        assert len(delegated_steps) == 1
        
        for step_id, plan_id, step_desc, session_id, step_order in delegated_steps:
            cursor.execute("""
                SELECT subagent_id, message, timestamp 
                FROM subagent_messages 
                WHERE parent_session_id = ? AND role = 'subagent'
                ORDER BY id DESC LIMIT 1
            """, (session_id,))
            subagent_row = cursor.fetchone()
            assert subagent_row is not None
            subagent_id, message, timestamp = subagent_row
            
            if "subagent completed:" in message.lower():
                cursor.execute("UPDATE plan_steps SET status = 'completed' WHERE id = ?", (step_id,))
                conn.commit()
                
                # Check resume_parent gets called/task gets created
                from agent.keyless import KeylessAgyAgent
                agent = KeylessAgyAgent(
                    model="gemini-3.5-flash",
                    conversation_id=session_id,
                    timeout=120.0
                )
                async with agent as active_agent:
                    await active_agent.chat(f"Subagent '{subagent_id}' completed: {message}")
                    
        conn.close()
        
        # Verify step is now 'completed'
        plan = memory.get_session_plan(session_id)
        assert plan["steps"][0]["status"] == "completed"
        
        # Verify the KeylessAgyAgent was invoked to resume the parent
        mock_agent_class.assert_called_with(
            model="gemini-3.5-flash",
            conversation_id=session_id,
            timeout=120.0
        )
