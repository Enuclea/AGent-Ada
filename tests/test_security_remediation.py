import pytest
pytestmark = pytest.mark.security
import os
import tempfile
from pathlib import Path
from unittest import mock
from agent import tools

def test_sandbox_fail_closed():
    # Ensure bypass is disabled for this test by mocking the frozen config constant
    with mock.patch("agent.execution.tools.security._ADA_DISABLE_SANDBOX_FROZEN", False), \
         mock.patch("shutil.which", return_value=None), \
         mock.patch("ctypes.util.find_library", return_value=None):
         
         # The fail-closed model must raise a PermissionError
         with pytest.raises(PermissionError) as exc_info:
             tools._sandbox_command_if_possible("whoami")
         assert "Sandbox environment could not be enforced" in str(exc_info.value)

def test_sandbox_explicit_bypass():
    # With bypass frozen flag mocked to True, it should return the command unmodified
    with mock.patch("agent.execution.tools.security._ADA_DISABLE_SANDBOX_FROZEN", True):
        cmd = tools._sandbox_command_if_possible("whoami")
        assert cmd == ["bash", "-c", "whoami"]

@pytest.mark.asyncio
async def test_install_skill_path_traversal_sanitization():
    # Attempting to install a skill name with traversal characters should be rejected immediately
    res1 = await tools.install_repository_skill("../traversal")
    assert "directory traversal attempt detected" in res1.lower()
    
    res2 = await tools.install_repository_skill("some/sub/folder")
    assert "directory traversal attempt detected" in res2.lower()

@pytest.mark.asyncio
async def test_local_skill_signature_enforcement():
    # Mock repositories lookup to return a local skill
    with tempfile.TemporaryDirectory() as td:
        local_path = Path(td) / "test-local-skill"
        local_path.mkdir()
        with open(local_path / "SKILL.md", "w") as f:
            f.write("---\nname: test-local-skill\ndescription: Test local skill\n---\n# Test local skill")
            
        local_skill_info = {
            "test-local-skill": {
                "name": "test-local-skill",
                "type": "hermes",
                "path": str(local_path),
                "remote": False,
                "description": "A test local skill package"
            }
        }
        
        with mock.patch("agent.tools._find_repository_skills", return_value=local_skill_info), \
             mock.patch("agent.execution.tools.security._verify_in_memory_signature", return_value=False), \
             mock.patch("agent.execution.tools.system_tools.spawn_subagent", return_value="DECISION: APPROVED"), \
             mock.patch.dict(os.environ, {"ADA_SKILL_INSTALL_CONFIRMED": "1"}):
             
             # Try installing the local skill — it must fail because signature verification returned False
             res = await tools.install_repository_skill("test-local-skill")
             assert "missing cryptographic signature" in res or "invalid cryptographic signature" in res

def test_extract_json_block():
    from agent.execution.tools.skills_tools import extract_json_block
    
    # 1. Markdown code block
    text1 = "Here is the response:\n```json\n{\n  \"safe\": true,\n  \"findings\": []\n}\n```\nHope that helps!"
    res1 = extract_json_block(text1)
    assert res1 is not None
    assert res1["safe"] is True
    
    # 2. Raw JSON string
    text2 = "Check this out: {\"safe\": false, \"findings\": [\"issue\"]}"
    res2 = extract_json_block(text2)
    assert res2 is not None
    assert res2["safe"] is False
    assert "issue" in res2["findings"]
    
    # 3. Invalid JSON
    text3 = "This is not json: {\"safe\": true"
    res3 = extract_json_block(text3)
    assert res3 is None

@pytest.mark.asyncio
async def test_consensus_escalation_triggers():
    # Test that secondary reviewer is triggered if primary reviewer flags HIL or is unsafe
    with tempfile.TemporaryDirectory() as td:
        local_path = Path(td) / "test-local-skill"
        local_path.mkdir()
        with open(local_path / "SKILL.md", "w") as f:
            f.write("---\nname: test-local-skill\ndescription: Test local skill\n---\n# Test local skill")
            
        local_skill_info = {
            "test-local-skill": {
                "name": "test-local-skill",
                "type": "hermes",
                "path": str(local_path),
                "remote": False,
                "description": "A test local skill package"
            }
        }
        
        # We will mock the AgyRoute to verify if it is called
        mock_agy_instance = mock.MagicMock()
        mock_agy_instance.execute = mock.AsyncMock()
        mock_agy_instance.execute.return_value = mock.MagicMock(response='```json\n{"safe": true, "findings": [], "requires_hil": false, "proceed_recommended": true}\n```')
        
        # Primary reviewer returns unsafe response
        primary_unsafe_response = '```json\n{"safe": false, "findings": ["subprocess used"], "requires_hil": true, "proceed_recommended": false}\n```'
        
        with mock.patch("agent.tools._find_repository_skills", return_value=local_skill_info), \
             mock.patch("agent.execution.tools.security._verify_in_memory_signature", return_value=True), \
             mock.patch("agent.execution.tools.system_tools.spawn_subagent", return_value=primary_unsafe_response), \
             mock.patch("agent.routes.agy.AgyRoute", return_value=mock_agy_instance), \
             mock.patch.dict(os.environ, {"ADA_SKILL_INSTALL_CONFIRMED": "1"}):
             
             res = await tools.install_repository_skill("test-local-skill")
             # Because primary said unsafe, it should trigger secondary and proceed since we mocked secondary to approve
             # and we set HIL confirmation to "1"
             assert "Successfully downloaded and installed skill" in res
             # Verify secondary (AgyRoute) was indeed executed
             mock_agy_instance.execute.assert_called_once()
