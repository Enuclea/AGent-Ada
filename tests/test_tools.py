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
        with mock.patch("agent.tools.SKILLS_DIR", tmp_path), \
             mock.patch("agent.tools.WORKSPACE_SKILLS_DIR", tmp_path / "workspace_skills"), \
             mock.patch("agent.tools.OPENCLAW_EXTS_DIR", tmp_path / "openclaw_exts"), \
             mock.patch("agent.tools.OPENCLAW_SKILLS_DIR", tmp_path / "openclaw_skills"), \
             mock.patch("agent.tools.HERMES_SKILLS_DIR", tmp_path / "hermes_skills"):
            yield tmp_path

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


def test_backup_discord_channel():
    """Test backup_discord_channel tool with mocked discord.Client."""
    import asyncio
    import datetime
    from unittest import mock
    import discord
    from agent import tools

    async def run_test():
        # 1. Setup mock message & channel
        mock_author = mock.MagicMock()
        mock_author.name = "TestUser"
        mock_author.display_name = "TestUser"
        mock_author.discriminator = "0"
        mock_author.__str__.return_value = "TestUser"

        mock_msg = mock.MagicMock()
        mock_msg.created_at = datetime.datetime(2026, 6, 22, 12, 0, 0)
        mock_msg.author = mock_author
        mock_msg.content = "Test message content"
        mock_msg.attachments = []
        mock_msg.embeds = []

        mock_channel = mock.MagicMock(spec=discord.TextChannel)
        mock_channel.name = "general"
        mock_channel.id = 12345
        
        async def mock_history(limit=None, oldest_first=True):
            yield mock_msg
            
        mock_channel.history.return_value = mock_history()

        # 2. Patch discord.Client.start, get_channel, and environments
        with mock.patch.dict("os.environ", {"DISCORD_BOT_TOKEN": "fake-token"}), \
             mock.patch("discord.Client.start", autospec=True) as mock_start, \
             mock.patch("discord.Client.get_channel", return_value=mock_channel), \
             mock.patch("builtins.open", mock.mock_open()) as mock_file:
             
            # When start is called, run the client's on_ready method
            async def side_effect(self, token):
                await self.on_ready()
            mock_start.side_effect = side_effect

            # Call the tool
            res = await tools.backup_discord_channel("12345")
            
            # Verify the result
            assert "Successfully backed up 1 messages" in res
            assert "general" in res
            
            # Verify file open and writes
            # Filter the calls to find the one where the backup file is opened for writing
            write_open_calls = [c for c in mock_file.call_args_list if len(c.args) > 1 and c.args[1] == 'w']
            assert len(write_open_calls) == 1
            
            # Verify the mock handle was written to with the expected format
            handle = mock_file()
            handle.write.assert_any_call("[2026-06-22 12:00:00] TestUser: Test message content\n")

    asyncio.run(run_test())


def test_youtube_to_mp3():
    """Test youtube_to_mp3 tool with mock yt_dlp."""
    from unittest import mock
    from pathlib import Path
    from agent import tools

    mock_ydl = mock.MagicMock()
    mock_info = {"title": "Test Song Title"}
    mock_ydl.extract_info.return_value = mock_info
    mock_ydl.prepare_filename.return_value = "/home/dan/AGent/share/data/mp3/Test Song Title.webm"

    with mock.patch("yt_dlp.YoutubeDL") as mock_ytdl_class, \
         mock.patch("pathlib.Path.mkdir") as mock_mkdir, \
         mock.patch("pathlib.Path.exists", return_value=True) as mock_exists:
        
        mock_ytdl_class.return_value.__enter__.return_value = mock_ydl
        
        res = tools.youtube_to_mp3("https://www.youtube.com/watch?v=123")
        
        mock_mkdir.assert_called()
        mock_ydl.extract_info.assert_called_once_with("https://www.youtube.com/watch?v=123", download=True)
        assert "Successfully downloaded and converted video to MP3" in res
        assert "🎵 **Song Title**: Test Song Title" in res
        assert "🔗 **Download URL**: https://10.250.1.200:8443/files/mp3/Test%20Song%20Title.mp3" in res


def test_scheduling_tools():
    """Test schedule_task, list_scheduled_tasks, and delete_scheduled_task tools."""
    from unittest import mock
    from agent import tools

    mock_add = mock.MagicMock()
    mock_get = mock.MagicMock(return_value=[
        {"id": "task-123", "name": "Test Task", "cron_expr": "*/5 * * * *", "next_run": "2026-06-23T10:00:00"}
    ])
    mock_delete = mock.MagicMock()

    with mock.patch("agent.memory.add_scheduled_task", mock_add), \
         mock.patch("agent.memory.get_scheduled_tasks", mock_get), \
         mock.patch("agent.memory.delete_scheduled_task", mock_delete):
         
        # Test schedule_task
        res = tools.schedule_task("Test Task", "Do something", "*/5 * * * *")
        assert "Successfully scheduled task" in res
        mock_add.assert_called_once()
        
        # Test list_scheduled_tasks
        res_list = tools.list_scheduled_tasks()
        assert "Active scheduled tasks" in res_list
        assert "Test Task" in res_list
        mock_get.assert_called_once()
        
        # Test delete_scheduled_task
        res_del = tools.delete_scheduled_task("task-123")
        assert "Successfully deleted scheduled task" in res_del
        mock_delete.assert_called_once_with("task-123")


def test_get_relevant_skills(temp_skills_dir):
    """Test getting relevant skills dynamically based on query prompt."""
    # List should be empty initially or show no custom skills
    res = tools.get_relevant_skills("mail-check")
    assert "No relevant custom skills found" in res

    # Create a skill with frontmatter description/category
    tools.create_agent_skill(
        skill_name="mail-check",
        description="Check email security and deliverability metrics",
        instructions="Perform SPF, DKIM, DMARC checks"
    )

    # Search for it
    res = tools.get_relevant_skills("Need to run mail-check tool for domain")
    assert "mail-check" in res
    assert "metrics" in res


@pytest.fixture
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





