import os
import tempfile
from pathlib import Path
import pytest

# Force a clean temporary database for web client testing to protect the user's live database
tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp_db_path = tmp_db.name
tmp_db.close()
os.environ["AGENT_DB_PATH"] = tmp_db_path

from fastapi.testclient import TestClient
import sqlite3
from unittest.mock import AsyncMock, patch
from agent import memory

# Override DB_FILE_PATH at the canonical source since memory module might have been loaded earlier
import agent.db
agent.db.DB_FILE_PATH = Path(tmp_db_path)
memory.init_db()

from agent.web import app
from fastapi.testclient import TestClient
client = TestClient(app)

def setup_module():
    # Ensure DB is initialized
    memory.init_db()
    # Clear any active tasks/schedules
    memory.clear_active_tasks()
    conn = sqlite3.connect(memory.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scheduled_tasks")
        cursor.execute("DELETE FROM task_logs")
        cursor.execute("DELETE FROM active_tasks")
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}

@patch("agent.web.get_or_create_agent")
def test_status_endpoint(mock_get_agent):
    mock_agent = AsyncMock()
    mock_agent.conversation_id = "test-session-123"
    mock_get_agent.return_value = mock_agent

    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "version" in data
    assert "workspace" in data
    assert data["session_id"] == "test-session-123"

def test_tasks_endpoint():
    # Add a mock active task
    memory.add_active_task("test-task-123", "test_command", "git status")
    
    response = client.get("/api/tasks")
    assert response.status_code == 200
    data = response.json()
    tasks = data.get("tasks", [])
    assert len(tasks) > 0
    assert any(t["id"] == "test-task-123" for t in tasks)

def test_task_logs_endpoints():
    task_id = "test-log-task"
    # Log progress
    response = client.post(f"/api/tasks/{task_id}/log", json={"message": "Initializing repo"})
    assert response.status_code == 200
    
    # Retrieve logs
    response = client.get(f"/api/tasks/{task_id}/logs")
    assert response.status_code == 200
    data = response.json()
    logs = data.get("logs", [])
    assert len(logs) == 1
    assert logs[0]["message"] == "Initializing repo"

def test_schedule_endpoints():
    # Schedule a task
    response = client.post("/api/schedule", json={
        "name": "Hourly Lint",
        "prompt": "Run pylint src/",
        "cron_expr": "60" # 60 seconds interval
    })
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    schedule_id = data["id"]
    assert "next_run" in data
    
    # List schedules
    response = client.get("/api/schedule")
    assert response.status_code == 200
    schedules = response.json().get("schedules", [])
    assert any(s["id"] == schedule_id for s in schedules)
    
    # Delete schedule
    response = client.delete(f"/api/schedule/{schedule_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    
    # Verify deletion
    response = client.get("/api/schedule")
    schedules = response.json().get("schedules", [])
    assert not any(s["id"] == schedule_id for s in schedules)


def test_fork_and_reload_endpoints():
    """Test the fork and reload capabilities in the web server."""
    # Write some conversation steps to test_db
    conn = sqlite3.connect(memory.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        # insert mockup rows for session "parent-session"
        for i in range(5):
            cursor.execute(
                "INSERT INTO conversation_steps (session_id, timestamp, role, content) VALUES (?, ?, ?, ?)",
                ("parent-session", f"2026-06-26T00:00:0{i}Z", "user" if i%2==0 else "assistant", f"Message {i}")
            )
        conn.commit()
    finally:
        conn.close()

    # Test Fork API
    fork_payload = {
        "session_id": "parent-session",
        "fork_step_index": 3
    }
    # Mock get_or_create_agent inside fork handler
    with patch("agent.web.get_or_create_agent") as mock_get_agent:
        response = client.post("/api/sessions/fork", json=fork_payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        new_sess_id = data["new_session_id"]
        assert new_sess_id is not None
        
        # Verify rows were copied to new session
        conn = sqlite3.connect(memory.DB_FILE_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT content FROM conversation_steps WHERE session_id = ? ORDER BY id ASC", (new_sess_id,))
            rows = cursor.fetchall()
            assert len(rows) == 3
            assert rows[0][0] == "Message 0"
            assert rows[1][0] == "Message 1"
            assert rows[2][0] == "Message 2"
        finally:
            conn.close()

    # Test Reload Chat Command
    reload_response = client.post("/api/chat", json={"prompt": "/reload", "session_id": "parent-session", "model": "gemini-3.5-flash"})
    assert reload_response.status_code == 200
    assert "Custom skills directory reloaded" in reload_response.text


def test_compaction_logic():
    """Test rolling context compaction logic in the database."""
    from unittest.mock import AsyncMock, patch, MagicMock
    import sqlite3
    from agent import memory
    
    conn = sqlite3.connect(memory.DB_FILE_PATH)
    session_id = "test-compaction-sess"
    try:
        cursor = conn.cursor()
        # Insert 65 mockup conversation steps for session
        for i in range(65):
            cursor.execute(
                "INSERT INTO conversation_steps (session_id, timestamp, role, content) VALUES (?, ?, ?, ?)",
                (session_id, f"2026-06-26T00:00:{i:02d}Z", "user" if i%2==0 else "assistant", f"Step Content {i}")
            )
        conn.commit()
    finally:
        conn.close()

    # Trigger compaction
    from agent.web import check_and_compact_session_history
    import asyncio
    
    # Mock model chat to return mock summary
    mock_chat_response = AsyncMock()
    # mock_chat_response generator for async for
    async def mock_async_iter(*args, **kwargs):
        yield "This is a context compression summary of first 40 steps."
    mock_chat_response.__aiter__ = mock_async_iter
    
    mock_agent_instance = MagicMock()
    mock_agent_instance.chat = AsyncMock(return_value=mock_chat_response)
    mock_agent_instance.__aenter__ = AsyncMock(return_value=mock_agent_instance)
    mock_agent_instance.__aexit__ = AsyncMock(return_value=None)
    
    with patch("agent.web.KeylessAgyAgent", return_value=mock_agent_instance):
        asyncio.run(check_and_compact_session_history(session_id, model_name="gemini-3.5-flash"))
        
    # Check that database has 65 - 40 + 1 = 26 steps
    conn = sqlite3.connect(memory.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT role, content FROM conversation_steps WHERE session_id = ? ORDER BY id ASC", (session_id,))
        rows = cursor.fetchall()
        assert len(rows) == 26
        # First row should be the thought compression summary
        assert rows[0][0] == "thought"
        assert "[System Context Compression Summary]" in rows[0][1]
        assert "This is a context compression summary" in rows[0][1]
        # Last row should be Step Content 64
        assert rows[-1][1] == "Step Content 64"
    finally:
        conn.close()

def teardown_module():
    try:
        os.remove(tmp_db_path)
    except OSError:
        pass

def test_conditional_tool_registration():
    """Verify that backup_discord_channel is conditionally registered in get_or_create_agent."""
    import inspect
    from agent import web
    
    source = inspect.getsource(web.get_or_create_agent)
    assert "tools.backup_discord_channel" in source
    assert "if not is_discord:" in source or "if not is_discord" in source

def test_repo_skills_endpoints():
    """Verify that the repository skills endpoints are exposed and return correct formats."""
    # 1. Test GET /api/repo-skills
    with patch("agent.tools._find_repository_skills") as mock_find:
        mock_find.return_value = {
            "test-skill": {
                "name": "test-skill",
                "type": "hermes",
                "path": Path("/tmp/test-skill"),
                "description": "A mock hermes skill"
            }
        }
        response = client.get("/api/repo-skills")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert len(data["skills"]) == 1
        assert data["skills"][0]["name"] == "test-skill"
        assert data["skills"][0]["type"] == "hermes"
        assert data["skills"][0]["description"] == "A mock hermes skill"

    # 2. Test GET /api/repo-skills/{name}/code
    with patch("agent.tools.view_repository_skill_code") as mock_view:
        mock_view.return_value = "=== Skill: test-skill ===\ncode contents"
        response = client.get("/api/repo-skills/test-skill/code")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "code contents" in data["code"]

        # Test error handling
        mock_view.return_value = "Error: Skill not found"
        response = client.get("/api/repo-skills/nonexistent/code")
        assert response.status_code == 404

    # 3. Test POST /api/repo-skills/{name}/install
    with patch("agent.tools.install_repository_skill") as mock_install:
        mock_install.return_value = "Successfully downloaded and installed skill test-skill"
        response = client.post("/api/repo-skills/test-skill/install")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "Successfully" in data["detail"]

        # Test error handling
        mock_install.return_value = "Error installing skill: perm denied"
        response = client.post("/api/repo-skills/test-skill/install")
        assert response.status_code == 400


def test_modules_endpoint():
    response = client.get("/api/modules")
    assert response.status_code == 200
    data = response.json()
    assert "modules" in data
    modules = data["modules"]
    assert isinstance(modules, list)





def test_install_skill_endpoint_path_traversal():
    '''Verify that installing a skill sanitizes name and prevents path traversal escaping SKILLS_DIR.'''
    from unittest.mock import patch
    from pathlib import Path
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_skills_dir = Path(tmp_dir) / "skills"
        tmp_skills_dir.mkdir()
        
        with patch("agent.tools.SKILLS_DIR", tmp_skills_dir):
            # 1. Test a valid skill installation
            payload = {
                "name": "My Safe Skill",
                "description": "Safe test skill",
                "instructions": "Run safety checks"
            }
            response = client.post("/api/skills/install", json=payload)
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "success"
            
            # Check file was created under our temp skills directory
            expected_file = tmp_skills_dir / "my_safe_skill" / "SKILL.md"
            assert expected_file.exists()
            content = expected_file.read_text()
            assert 'name: "My Safe Skill"' in content
            
            # 2. Test sanitization of name (e.g. replacing '..' with '.')
            payload_with_dots = {
                "name": "My..Safe..Skill",
                "description": "Safe test skill",
                "instructions": "Run safety checks"
            }
            response = client.post("/api/skills/install", json=payload_with_dots)
            assert response.status_code == 200
            expected_file_dots = tmp_skills_dir / "my.safe.skill" / "SKILL.md"
            assert expected_file_dots.exists()

            # 3. Test path traversal attempt that results in escaping SKILLS_DIR
            bad_payload = {
                "name": "..",
                "description": "Exploit attempt",
                "instructions": "Execute malware"
            }
            response = client.post("/api/skills/install", json=bad_payload)
            assert response.status_code == 400
            assert "escapes the skills directory" in response.json()["detail"]

def test_cancel_session_endpoint():
    response = client.post("/api/sessions/test-cancel-sess/cancel")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "Execution stopped." in data["message"]
