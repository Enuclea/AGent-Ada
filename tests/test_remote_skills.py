import json
import urllib.request
import tempfile
import asyncio
from pathlib import Path
from unittest import mock
import pytest

from agent import tools

@pytest.fixture
def temp_skills_dir():
    """Fixture that redirects SKILLS_DIR to a temporary directory."""
    import os
    old_env = os.environ.get("ADA_SKILL_INSTALL_CONFIRMED")
    os.environ["ADA_SKILL_INSTALL_CONFIRMED"] = "1"
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            with mock.patch("agent.tools.SKILLS_DIR", tmp_path), \
                 mock.patch("agent.tools.WORKSPACE_SKILLS_DIR", tmp_path / "workspace_skills"), \
                 mock.patch("agent.tools.OPENCLAW_EXTS_DIR", tmp_path / "openclaw_exts"), \
                 mock.patch("agent.tools.OPENCLAW_SKILLS_DIR", tmp_path / "openclaw_skills"), \
                 mock.patch("agent.tools.HERMES_SKILLS_DIR", tmp_path / "hermes_skills"):
                yield tmp_path
    finally:
        if old_env is None:
            os.environ.pop("ADA_SKILL_INSTALL_CONFIRMED", None)
        else:
            os.environ["ADA_SKILL_INSTALL_CONFIRMED"] = old_env

def test_find_remote_repository_skills_disabled(temp_skills_dir):
    """Test that remote skill repositories are no longer searched or fetched."""
    # Mock urlopen to fail if any remote HTTP requests are attempted
    def mock_urlopen(req, timeout=None):
        raise AssertionError("Urllib.request.urlopen should NOT be called since remote repositories are disabled.")

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        skills = tools._find_repository_skills()
        # Verify no remote fields exist, and only local skills/extensions are returned
        for s_info in skills.values():
            assert not s_info.get("remote", False)

def test_view_remote_skill_code_disabled(temp_skills_dir):
    """Test that viewing remote skill code returns a disabled error message."""
    mock_repo_skills = {
        "remote-skill": {
            "name": "remote-skill",
            "type": "openclaw",
            "remote": True
        }
    }
    with mock.patch("agent.tools._find_repository_skills", return_value=mock_repo_skills):
        res = tools.view_repository_skill_code("remote-skill")
        assert "Remote repository fetching is disabled." in res

def test_install_remote_skill_disabled(temp_skills_dir):
    """Test that installing a remote skill returns a disabled error message."""
    mock_repo_skills = {
        "remote-skill": {
            "name": "remote-skill",
            "type": "openclaw",
            "remote": True
        }
    }
    async def run_test():
        with mock.patch("agent.tools._find_repository_skills", return_value=mock_repo_skills):
            res = await tools.install_repository_skill("remote-skill")
            assert "Remote repository fetching is disabled." in res

    asyncio.run(run_test())
