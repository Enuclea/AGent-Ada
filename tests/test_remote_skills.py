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

def test_find_remote_repository_skills(temp_skills_dir):
    """Test that remote skills from ClawHub and Hermes index are fetched and listed correctly."""
    mock_clawhub_response = {
        "items": [
            {
                "slug": "test-remote-claw",
                "displayName": "Test Remote Claw",
                "summary": "A mock remote OpenClaw skill."
            }
        ]
    }
    
    mock_hermes_response = {
        "skills": [
            {
                "name": "test-remote-hermes",
                "description": "A mock remote Hermes skill.",
                "identifier": "official/test-remote-hermes",
                "repo": "NousResearch/hermes-agent",
                "path": "optional-skills/test-remote-hermes",
                "source": "official"
            }
        ]
    }

    def mock_urlopen(req, timeout=None):
        url = req.full_url if isinstance(req, urllib.request.Request) else req
        class MockResponse:
            def __init__(self, data):
                if isinstance(data, bytes):
                    self.data = data
                else:
                    self.data = json.dumps(data).encode("utf-8")
            def read(self):
                return self.data
            def decode(self, encoding="utf-8", errors="replace"):
                return self.data.decode(encoding, errors)
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
        
        if "api/v1/skills?limit=" in url:
            return MockResponse(mock_clawhub_response)
        elif "skills-index.json" in url:
            return MockResponse(mock_hermes_response)
        elif "skills/test-remote-claw" in url:
            if url.endswith("/versions/1.0.0"):
                return MockResponse({
                    "files": [
                        {"path": "SKILL.md", "content": "Remote Claw Instructions."}
                    ]
                })
            return MockResponse({
                "latestVersion": {"version": "1.0.0"}
            })
        elif "optional-skills/test-remote-hermes/SKILL.md" in url:
            return MockResponse(b"Remote Hermes Instructions.")
        raise ValueError(f"Unexpected url: {url}")

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        # Clear cache to force fetching
        cache_file = Path.home() / ".agent" / "cache" / "remote_repository_skills.json"
        if cache_file.exists():
            try:
                cache_file.unlink()
            except Exception:
                pass
            
        skills = tools._find_repository_skills()
        
        assert "test-remote-claw" in skills
        assert skills["test-remote-claw"]["type"] == "openclaw"
        assert skills["test-remote-claw"]["description"] == "A mock remote OpenClaw skill."
        
        assert "test-remote-hermes" in skills
        assert skills["test-remote-hermes"]["type"] == "hermes"
        assert skills["test-remote-hermes"]["description"] == "A mock remote Hermes skill."
        
        claw_code = tools.view_repository_skill_code("test-remote-claw")
        assert "Remote Claw Instructions." in claw_code
        
        hermes_code = tools.view_repository_skill_code("test-remote-hermes")
        assert "Remote Hermes Instructions." in hermes_code

def test_install_remote_repository_skill(temp_skills_dir):
    """Test installing a remote skill from the web store."""
    mock_clawhub_response = {
        "items": [
            {
                "slug": "test-remote-claw",
                "displayName": "Test Remote Claw",
                "summary": "A mock remote OpenClaw skill."
            }
        ]
    }
    
    def mock_urlopen(req, timeout=None):
        url = req.full_url if isinstance(req, urllib.request.Request) else req
        class MockResponse:
            def __init__(self, data_bytes_or_dict):
                if isinstance(data_bytes_or_dict, bytes):
                    self.data = data_bytes_or_dict
                else:
                    self.data = json.dumps(data_bytes_or_dict).encode("utf-8")
            def read(self):
                return self.data
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
            
        if "api/v1/skills?limit=" in url:
            return MockResponse(mock_clawhub_response)
        elif "skills/test-remote-claw" in url:
            if url.endswith("/versions/1.0.0"):
                return MockResponse({
                    "files": [
                        {"path": "SKILL.md", "content": "Claw Instructions."},
                        {"path": "scripts/test.py", "content": "print('hello')"}
                    ]
                })
            return MockResponse({
                "latestVersion": {"version": "1.0.0"}
            })
        raise ValueError(f"Unexpected url: {url}")
        
    async def run_test():
        with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen), \
             mock.patch("agent.execution.tools.spawn_subagent", return_value="DECISION: APPROVED"), \
             mock.patch("agent.execution.tools._verify_skill_signature", return_value=True):
            # Clear cache to force fetching
            cache_file = Path.home() / ".agent" / "cache" / "remote_repository_skills.json"
            if cache_file.exists():
                try:
                    cache_file.unlink()
                except Exception:
                    pass
                
            res = await tools.install_repository_skill("test-remote-claw")
            assert "Successfully downloaded and installed skill" in res
            
            # Verify it was written to SKILLS_DIR
            installed_path = temp_skills_dir / "test-remote-claw"
            assert installed_path.exists()
            assert (installed_path / "SKILL.md").exists()
            assert (installed_path / "scripts" / "test.py").exists()
            with open(installed_path / "SKILL.md", "r") as f:
                assert "Claw Instructions." in f.read()

    asyncio.run(run_test())
