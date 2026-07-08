import os
import tempfile
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock

try:
    import enuclea
except ImportError:
    enuclea = None

pytestmark = pytest.mark.skipif(
    enuclea is None,
    reason="Private enuclea package not available"
)

# Force a clean temporary database for web client testing to protect the user's live database
tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp_db_path = tmp_db.name
tmp_db.close()

from agent import memory
import agent.db
# Override DB_FILE_PATH at the canonical source
agent.db.DB_FILE_PATH = Path(tmp_db_path)
memory.init_db()

from agent.web import app

def setup_module():
    pass

def teardown_module():
    try:
        os.remove(tmp_db_path)
    except OSError:
        pass


def test_thumbtack_webhook_endpoints():
    """Test Thumbtack webhook integration endpoints (create and merge scenarios)."""
    import tempfile
    import os
    from unittest.mock import AsyncMock, patch, MagicMock
    from enuclea import db
    
    # Create a temp DB file to isolate tests
    fd, temp_db_path = tempfile.mkstemp()
    os.close(fd)
    db.init_db(temp_db_path)
    
    try:
        # Mock Thumbtack Discord Analysis response from Gemini
        async def mock_structured_output():
            return {
                "is_thumbtack_lead": True,
                "lead_name": "Jane Doe",
                "lead_grade": "A",
                "go_no_go": "Go",
                "technical_insights": "Wants a full house painting",
                "task_title": "[Thumbtack Lead - Go] Grade: A | Jane Doe - House Painting",
                "task_description": "Project details: full house painting."
            }
        
        mock_chat_response = AsyncMock()
        mock_chat_response.structured_output = mock_structured_output
        
        mock_agent_instance = MagicMock()
        mock_agent_instance.chat = AsyncMock(return_value=mock_chat_response)
        
        # Mock credentials
        mock_creds = ("mock-key", "mock-account")
        
        # Setup mock db connection
        def mock_get_db_connection(db_path=None):
            import sqlite3
            conn = sqlite3.connect(temp_db_path)
            conn.row_factory = sqlite3.Row
            return conn

        # We patch everything needed:
        with patch("enuclea.db.get_db_connection", side_effect=mock_get_db_connection), \
             patch("enuclea.keyless.KeylessAgyAgent", return_value=mock_agent_instance), \
             patch("enuclea.gmail_tool.load_morgen_credentials", return_value=mock_creds), \
             patch("enuclea.morgen_tool.load_morgen_credentials", return_value=mock_creds), \
             patch("enuclea.morgen_client.MorgenClient") as mock_morgen_client_cls, \
             patch("enuclea.morgen_tool.MorgenClient") as mock_morgen_client_cls2:
            
            with TestClient(app) as local_client:
                # Setup mock client
                mock_client = AsyncMock()
                mock_client.create_task.return_value = "morgen-task-123"
                mock_client.update_task.return_value = None
                mock_morgen_client_cls.return_value.__aenter__.return_value = mock_client
                mock_morgen_client_cls2.return_value.__aenter__.return_value = mock_client
                
                payload = {
                    "content": "New Thumbtack lead from Jane Doe. Wants house painting.",
                    "author": "Webhook#0000",
                    "channel_id": "1518534351002927205",
                    "message_id": "999888777",
                    "created_at": "2026-06-22T09:00:00Z"
                }
                
                # Test Scenario 1: CREATE
                response = local_client.post("/api/integrations/thumbtack", json=payload)
                print(f"RESPONSE STATUS: {response.status_code}")
                print(f"RESPONSE TEXT: {response.text}")
                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "created"
                assert data["lead_name"] == "Jane Doe"
                
                # Verify it saved to DB
                existing = db.find_matching_lead_task("Jane Doe", "thumbtack", db_path=temp_db_path)
                assert existing is not None
                assert existing["id"] == "morgen-task-123"
                assert "Jane Doe" in existing["title"]
                
                # Test Scenario 2: MERGE (running same payload again should merge because task already exists)
                response2 = local_client.post("/api/integrations/thumbtack", json=payload)
                assert response2.status_code == 200
                data2 = response2.json()
                assert data2["status"] == "merged"
                assert data2["task_id"] == "morgen-task-123"
                
                # Verify description was appended/updated in DB
                updated_task = db.find_matching_lead_task("Jane Doe", "thumbtack", db_path=temp_db_path)
                assert "Discord Webhook Update" in updated_task["description"]
            
    finally:
        try:
            os.remove(temp_db_path)
        except OSError:
            pass
