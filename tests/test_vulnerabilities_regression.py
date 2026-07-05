import os
import re
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock
from unittest.mock import patch, MagicMock, AsyncMock
import pytest
from fastapi.testclient import TestClient

from agent import memory
from agent import db
from agent.routes.agy import AgyRoute
from agent.routes.grok import GrokRoute
from agent.web import app

@pytest.fixture
def temp_env():
    memory.global_cache.clear()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "memory.json"
        tmp_db_path = Path(tmpdir) / "history.db"
        with mock.patch("agent.persistence.MEMORY_FILE_PATH", tmp_path), \
             mock.patch("agent.db.DB_FILE_PATH", tmp_db_path), \
             mock.patch("agent.tools.SKILLS_DIR", Path(tmpdir) / "skills"), \
             mock.patch("agent.interfaces.web.tools.SKILLS_DIR", Path(tmpdir) / "skills"):
            
            # Initialize temp DB
            memory.init_db()
            yield {
                "memory_path": tmp_path,
                "db_path": tmp_db_path,
                "skills_dir": Path(tmpdir) / "skills"
            }
    memory.global_cache.clear()

# 1. Path traversal in skill installation (/api/skills/install)
def test_skill_install_path_traversal(temp_env):
    client = TestClient(app)
    
    # Check that traversal attempts (e.g. using .. as a name) are rejected with HTTP 400
    response = client.post("/api/skills/install", json={
        "name": "..",
        "description": "desc",
        "instructions": "instructions"
    })
    assert response.status_code == 400
    assert "escapes" in response.json()["detail"].lower() or "invalid" in response.json()["detail"].lower()

    # Check another traversal name that would result in empty or invalid folder
    response2 = client.post("/api/skills/install", json={
        "name": "///",
        "description": "desc",
        "instructions": "instructions"
    })
    assert response2.status_code == 400

# 2. Route injection in AgyRoute and GrokRoute
@pytest.mark.anyio
async def test_route_injection_validation():
    routes = [AgyRoute(), GrokRoute()]
    for route in routes:
        # Invalid model starting with a hyphen
        with pytest.raises(ValueError, match="model cannot start with a hyphen"):
            await route.execute(prompt="test", model="-some-model")
            
        # Invalid model with regex violation
        with pytest.raises(ValueError, match="Invalid model"):
            await route.execute(prompt="test", model="model;injection")
            
        with pytest.raises(ValueError, match="Invalid model"):
            await route.execute(prompt="test", model="model' OR '1'='1")

        # Invalid conversation_id starting with a hyphen
        with pytest.raises(ValueError, match="conversation_id cannot start with a hyphen"):
            await route.execute(prompt="test", model="valid-model", conversation_id="-conv")
            
        # Invalid conversation_id with regex violation
        with pytest.raises(ValueError, match="Invalid conversation_id"):
            await route.execute(prompt="test", model="valid-model", conversation_id="conv;injection")

        with pytest.raises(ValueError, match="Invalid conversation_id"):
            await route.execute(prompt="test", model="valid-model", conversation_id="conv' OR '1'='1")

# 3. SQL injection via database compaction
def test_compact_all_memories_sql_injection(temp_env):
    db_path = temp_env["db_path"]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Insert a safe task and an unsafe task mimicking SQL injection
    safe_task_id = "safe-task-123"
    unsafe_task_id = "unsafe-id' OR '1'='1"
    
    # We must match the table structure of active_tasks
    cursor.execute("""
        INSERT INTO active_tasks (id, status, started_at) 
        VALUES (?, 'completed', '2026-07-05 00:00:00')
    """, (safe_task_id,))
    
    cursor.execute("""
        INSERT INTO active_tasks (id, status, started_at) 
        VALUES (?, 'completed', '2026-07-05 00:01:00')
    """, (unsafe_task_id,))
    
    conn.commit()
    conn.close()
    
    # Run memory compaction
    stats = memory.compact_all_memories()
    
    # Re-verify database state
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM active_tasks")
    remaining_ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    # The safe task should still exist
    assert safe_task_id in remaining_ids
    
    # The unsafe task should have been filtered out and therefore DELETED (since it was not in the keep list)
    assert unsafe_task_id not in remaining_ids
