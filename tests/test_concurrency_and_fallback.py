import os
import json
import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch
from agent.merge import merge_text
from agent import tools

def test_concurrent_conflict_resolution():
    """Verify that actual conflicting edits on the same line result in a conflict."""
    base = "original line 1\noriginal line 2\n"
    # Specialist A edits line 1
    a = "specialist A line 1\noriginal line 2\n"
    # Specialist B edits line 1 differently
    b = "specialist B line 1\noriginal line 2\n"
    
    merged, conflict = merge_text(base, a, b, "SPECIALIST_A", "SPECIALIST_B")
    assert conflict
    assert "<<<<<<< SPECIALIST_A" in merged
    assert "=======" in merged
    assert ">>>>>>> SPECIALIST_B" in merged

def test_concurrent_clean_merge():
    """Verify that non-conflicting edits to different lines merge cleanly."""
    base = "line 1\nline 2\nline 3\n"
    # Specialist A edits line 1
    a = "line 1 edited\nline 2\nline 3\n"
    # Specialist B edits line 3
    b = "line 1\nline 2\nline 3 edited\n"
    
    merged, conflict = merge_text(base, a, b, "SPECIALIST_A", "SPECIALIST_B")
    assert not conflict
    assert merged == "line 1 edited\nline 2\nline 3 edited\n"

@pytest.mark.anyio
async def test_boardroom_failed_consensus_fallback():
    """Verify that the boardroom aborts and does not apply sandbox changes if consensus fails."""
    with patch("agent.keyless.KeylessAgyAgent") as mock_agent_class, \
         patch("agent.registry.tool_registry.resolve_subagent_profile") as mock_resolve, \
         patch("agent.memory.active_session_id_var") as mock_session_var:
         
        mock_session_var.get.return_value = "parent-sess"
        mock_resolve.return_value = "Expert Profile Details"
        
        # Mock agent to return failure (approved=False) on all rounds
        mock_agent = AsyncMock()
        mock_agent_class.return_value = mock_agent
        mock_conn = AsyncMock()
        mock_agent.__aenter__.return_value = mock_conn
        
        async def mock_chat(prompt):
            yield '{"approved": false, "critique_or_comments": "Disapprove", "updated_solution_summary": "Attempted edit", "files_modified": ["src/app.py"]}'
        mock_conn.chat.side_effect = mock_chat
        
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                # Setup target file in workspace
                app_file = Path(tmpdir) / "src" / "app.py"
                app_file.parent.mkdir(parents=True, exist_ok=True)
                app_file.write_text("print('original')")
                
                # Run boardroom
                res = await tools.run_boardroom(
                    task_description="Modify app.py",
                    expert_profiles=["Linter"],
                    target_files=["src/app.py"]
                )
                
                res_data = json.loads(res)
                assert res_data["status"] == "failure"
                assert "exceeded max rounds" in res_data["validation_result"].lower()
                assert res_data["files_modified"] == []
                
                # Verify that the active workspace file was NOT modified/copied
                assert app_file.read_text() == "print('original')"
            finally:
                os.chdir(old_cwd)
