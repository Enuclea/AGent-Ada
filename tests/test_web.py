import pytest
from fastapi.testclient import TestClient
import sqlite3
from unittest.mock import AsyncMock, patch
from agent.web import app
from agent import memory

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
