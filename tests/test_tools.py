import tempfile
from pathlib import Path
from unittest import mock
import pytest

from agent import tools

@pytest.fixture
def temp_skills_dir():
    """Fixture that redirects SKILLS_DIR to a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        with mock.patch("agent.tools.SKILLS_DIR", tmp_path):
            yield tmp_path

@pytest.fixture(autouse=True)
def mock_external_dirs():
    """Redirects external repository paths to empty temporary directories for all tests."""
    with tempfile.TemporaryDirectory() as td1, \
         tempfile.TemporaryDirectory() as td2, \
         tempfile.TemporaryDirectory() as td3:
         
         paths = {
             "openclaw_exts": Path(td1),
             "openclaw_skills": Path(td2),
             "hermes_skills": Path(td3)
         }
         with mock.patch("agent.tools.OPENCLAW_EXTS_DIR", paths["openclaw_exts"]), \
              mock.patch("agent.tools.OPENCLAW_SKILLS_DIR", paths["openclaw_skills"]), \
              mock.patch("agent.tools.HERMES_SKILLS_DIR", paths["hermes_skills"]):
              yield paths

def test_create_and_list_skills(temp_skills_dir):
    """Test creating a skill and listing it afterwards."""
    # List should be empty initially
    res_list = tools.list_installed_skills()
    assert "No custom skills installed" in res_list

    # Create a skill
    res_create = tools.create_agent_skill(
        skill_name="test-workflow",
        description="Verifies unit testing workflow",
        instructions="1. Run pytest\n2. Check coverage",
        script_content="import sys\nprint('running workflow')\nsys.exit(0)",
        script_filename="run.py"
    )
    assert "Successfully created skill" in res_create
    assert "test-workflow" in res_create

    # Check directory structure
    skill_path = temp_skills_dir / "test-workflow"
    assert skill_path.exists()
    assert (skill_path / "SKILL.md").exists()
    
    script_path = skill_path / "scripts" / "run.py"
    assert script_path.exists()
    with open(script_path, "r", encoding="utf-8") as f:
        assert "running workflow" in f.read()

    # List again to verify it is discovered
    res_list_after = tools.list_installed_skills()
    assert "test-workflow" in res_list_after
    assert "Verifies unit testing workflow" in res_list_after

def test_improve_skill(temp_skills_dir):
    """Test editing and improving an existing skill."""
    res_fail = tools.improve_agent_skill(
        skill_name="nonexistent-skill",
        description="Overriding description"
    )
    assert "does not exist" in res_fail

    tools.create_agent_skill(
        skill_name="my-skill",
        description="Original description",
        instructions="Original instructions"
    )

    res_success = tools.improve_agent_skill(
        skill_name="my-skill",
        description="New description",
        instructions="New instructions"
    )
    assert "Successfully updated skill" in res_success

    skill_md = temp_skills_dir / "my-skill" / "SKILL.md"
    assert skill_md.exists()
    with open(skill_md, "r", encoding="utf-8") as f:
        content = f.read()
        assert "New description" in content
        assert "New instructions" in content
        assert "Original description" not in content

def test_search_past_conversations_tool():
    """Test search_past_conversations tool outputs correct matching lines."""
    mock_results = [
        {"session_id": "session1", "role": "user", "content": "Query test content", "tool_name": None},
        {"session_id": "session1", "role": "tool_call", "content": "git diff", "tool_name": "run_command"}
    ]
    with mock.patch("agent.memory.search_conversations", return_value=mock_results):
        res = tools.search_past_conversations("some query")
        assert "Found 2 matches" in res
        assert "Session: session1" in res
        assert "[USER]: Query test content" in res
        assert "[TOOL_CALL] Tool Call: run_command(git diff)" in res

def test_repository_skills(temp_skills_dir, mock_external_dirs):
    """Test list, view, and install tools for external repositories."""
    hermes_path = mock_external_dirs["hermes_skills"]
    openclaw_path = mock_external_dirs["openclaw_exts"]
    
    # Verify paths exists
    paths = tools.get_skills_paths()
    assert hermes_path in paths
    assert openclaw_path in paths
    
    # Set up a mock Hermes skill
    skill_folder = hermes_path / "apple" / "apple-notes"
    skill_folder.mkdir(parents=True, exist_ok=True)
    skill_md_content = "---\nname: apple-notes\ndescription: Manage Apple Notes\n---\n# Apple Notes\nInstructions here."
    with open(skill_folder / "SKILL.md", "w") as f:
        f.write(skill_md_content)
        
    # Set up a mock OpenClaw plugin
    plugin_folder = openclaw_path / "discord"
    plugin_folder.mkdir(parents=True, exist_ok=True)
    package_json_content = '{"name": "@openclaw/discord", "description": "Discord plugin"}'
    with open(plugin_folder / "package.json", "w") as f:
        f.write(package_json_content)
    plugin_json_content = '{"id": "discord"}'
    with open(plugin_folder / "openclaw.plugin.json", "w") as f:
        f.write(plugin_json_content)
        
    # Verify list_repository_skills
    list_res = tools.list_repository_skills()
    assert "apple-notes (hermes): Manage Apple Notes" in list_res
    assert "discord (openclaw): Discord plugin" in list_res
    
    # Verify view_repository_skill_code
    view_res = tools.view_repository_skill_code("apple-notes")
    assert "=== Skill: apple-notes (hermes) ===" in view_res
    assert "Manage Apple Notes" in view_res
    assert "Instructions here." in view_res
    
    # Verify install_repository_skill
    install_res = tools.install_repository_skill("apple-notes")
    assert "Successfully downloaded and installed skill" in install_res
    
    installed_skill_folder = temp_skills_dir / "apple-notes"
    assert installed_skill_folder.exists()
    assert (installed_skill_folder / "SKILL.md").exists()
    with open(installed_skill_folder / "SKILL.md", "r") as f:
        assert "Manage Apple Notes" in f.read()

