import pytest
import os
import tempfile
from pathlib import Path
from unittest import mock
from agent import tools

def test_sandbox_fail_closed():
    # Ensure bypass is disabled for this test
    with mock.patch.dict(os.environ, {"ADA_DISABLE_SANDBOX": ""}), \
         mock.patch("shutil.which", return_value=None), \
         mock.patch("ctypes.util.find_library", return_value=None):
         
         # The fail-closed model must raise a PermissionError
         with pytest.raises(PermissionError) as exc_info:
             tools._sandbox_command_if_possible("whoami")
         assert "Sandbox environment could not be enforced" in str(exc_info.value)

def test_sandbox_explicit_bypass():
    # With bypass env var set, it should return the command unmodified
    with mock.patch.dict(os.environ, {"ADA_DISABLE_SANDBOX": "1"}):
        cmd = tools._sandbox_command_if_possible("whoami")
        assert cmd == "whoami"

@pytest.mark.asyncio
async def test_install_skill_path_traversal_sanitization():
    # Attempting to install a skill name with traversal characters should be rejected immediately
    res1 = await tools.install_repository_skill("../traversal")
    assert "directory traversal attempt detected" in res1.lower()
    
    res2 = await tools.install_repository_skill("some/sub/folder")
    assert "directory traversal attempt detected" in res2.lower()

@pytest.mark.asyncio
async def test_remote_skill_signature_enforcement():
    # Mock repositories lookup to return a remote skill
    remote_skill_info = {
        "test-remote-skill": {
            "name": "test-remote-skill",
            "type": "openclaw",
            "identifier": "test-slug",
            "remote": True,
            "description": "A test remote skill package"
        }
    }
    
    with mock.patch("agent.execution.tools._find_repository_skills", return_value=remote_skill_info), \
         mock.patch("agent.execution.tools._verify_skill_signature", return_value=False), \
         mock.patch("urllib.request.urlopen") as mock_url_open, \
         mock.patch("agent.execution.tools.spawn_subagent", return_value="DECISION: APPROVED"), \
         mock.patch.dict(os.environ, {"ADA_SKILL_INSTALL_CONFIRMED": "1"}):
         
         # Mock API returns for Clawhub
         mock_resp = mock.MagicMock()
         mock_resp.read.return_value = b'{"latestVersion": {"version": "1.0.0"}, "files": []}'
         mock_url_open.return_value.__enter__.return_value = mock_resp
         
         # Try installing the remote skill — it must fail because signature verification returned False
         res = await tools.install_repository_skill("test-remote-skill")
         assert "missing cryptographic signature" in res or "invalid cryptographic signature" in res
