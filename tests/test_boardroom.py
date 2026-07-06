import os
import json
import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from agent import tools

@pytest.mark.anyio
async def test_boardroom_consensus_success():
    with patch("agent.keyless.KeylessAgyAgent") as mock_agent_class, \
         patch("agent.registry.tool_registry.resolve_subagent_profile") as mock_resolve, \
         patch("agent.memory.active_session_id_var") as mock_session_var:
         
        mock_session_var.get.return_value = "parent-sess"
        mock_resolve.return_value = "Expert Instructions"
        
        mock_agent = AsyncMock()
        mock_agent_class.return_value = mock_agent
        
        mock_conn = AsyncMock()
        mock_agent.__aenter__.return_value = mock_conn
        
        async def mock_chat(prompt):
            yield '{"approved": true, "critique_or_comments": "Good", "updated_solution_summary": "Done", "files_modified": ["test.py"]}'
        mock_conn.chat.side_effect = mock_chat
        
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                test_file = Path(tmpdir) / "test.py"
                test_file.write_text("print('hello')")
                
                res = await tools.run_boardroom(
                    task_description="Refactor test",
                    expert_profiles=["Linter"],
                    target_files=["test.py"]
                )
                
                data = json.loads(res)
                assert data["status"] == "success"
                assert "test.py" in data["files_modified"]
            finally:
                os.chdir(old_cwd)

@pytest.mark.anyio
async def test_boardroom_no_consensus_failure():
    with patch("agent.keyless.KeylessAgyAgent") as mock_agent_class, \
         patch("agent.registry.tool_registry.resolve_subagent_profile") as mock_resolve, \
         patch("agent.memory.active_session_id_var") as mock_session_var:
         
        mock_session_var.get.return_value = "parent-sess"
        mock_resolve.return_value = "Expert Instructions"
        
        mock_agent = AsyncMock()
        mock_agent_class.return_value = mock_agent
        
        mock_conn = AsyncMock()
        mock_agent.__aenter__.return_value = mock_conn
        
        async def mock_chat(prompt):
            yield '{"approved": false, "critique_or_comments": "Needs work", "updated_solution_summary": "Tried", "files_modified": ["test.py"]}'
        mock_conn.chat.side_effect = mock_chat
        
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                test_file = Path(tmpdir) / "test.py"
                test_file.write_text("print('hello')")
                
                res = await tools.run_boardroom(
                    task_description="Refactor test",
                    expert_profiles=["Linter"],
                    target_files=["test.py"]
                )
                
                data = json.loads(res)
                assert data["status"] == "failure"
                assert data["validation_result"] == "Boardroom debate exceeded max rounds without consensus."
                assert data["files_modified"] == []
            finally:
                os.chdir(old_cwd)

@pytest.mark.anyio
async def test_boardroom_frontier_model_routing():
    """
    Verifies that when expert profiles are Claude, DeepSeek, and Grok,
    the boardroom invokes KeylessAgyAgent with their corresponding Magica frontier models.
    """
    with patch("agent.keyless.KeylessAgyAgent") as mock_agent_class, \
         patch("agent.registry.tool_registry.resolve_subagent_profile") as mock_resolve, \
         patch("agent.memory.active_session_id_var") as mock_session_var:
         
        mock_session_var.get.return_value = "parent-sess"
        mock_resolve.return_value = "Expert Instructions"
        
        captured_models = []
        
        def mock_init(*args, **kwargs):
            captured_models.append(kwargs.get("model"))
            mock_agent = MagicMock()
            
            async def enter(*args, **kwargs):
                mock_conn = AsyncMock()
                async def mock_chat(prompt):
                    yield '{"approved": true, "critique_or_comments": "Looks good", "updated_solution_summary": "Done", "files_modified": []}'
                mock_conn.chat.side_effect = mock_chat
                return mock_conn
                
            mock_agent.__aenter__ = enter
            return mock_agent

        mock_agent_class.side_effect = mock_init
        
        await tools.run_boardroom(
            task_description="Review code",
            expert_profiles=["Claude", "DeepSeek", "Grok"],
            model="magica/default"
        )
        
        assert "magica/claude-opus-4-8" in captured_models
        assert "magica/deepseek-v3.2" in captured_models
        assert "magica/grok-4.3" in captured_models
