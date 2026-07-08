import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import os
import sqlite3
import tempfile
import asyncio
from datetime import datetime, timedelta
try:
    from enuclea import db
    from enuclea.morgen_tool import add_morgen_task, run_sync_and_get_changes, sync_morgen_tasks
    from enuclea.morgen_client import MorgenClient, MorgenClientError
    has_enuclea = True
except ImportError:
    has_enuclea = False

import pytest
if not has_enuclea:
    pytestmark = pytest.mark.skip(reason="enuclea private module not available")

class TestEnucleaMorgen(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Create a temporary file for the SQLite database
        self.db_fd, self.db_path = tempfile.mkstemp()
        db.init_db(self.db_path)

    def tearDown(self):
        # Close and remove the temporary database file
        os.close(self.db_fd)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_init_db(self):
        # Verify tables are created correctly
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='morgen_tasks'")
        table_exists = cursor.fetchone()
        self.assertIsNotNone(table_exists)
        conn.close()

    def test_add_task_db(self):
        # Test adding task directly in DB
        db.add_task(
            task_id="task-123",
            title="Test Task",
            description="Testing DB write",
            priority="High",
            db_path=self.db_path
        )
        
        task = db.get_task("task-123", db_path=self.db_path)
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "Test Task")
        self.assertEqual(task["priority"], "High")
        self.assertEqual(task["is_scheduled"], 0)
        self.assertEqual(task["status"], "needs-action")

    @patch("enuclea.morgen_tool.load_morgen_credentials")
    @patch("enuclea.morgen_tool.MorgenClient")
    async def test_add_morgen_task_tool(self, mock_client_cls, mock_credentials):
        mock_credentials.return_value = ("fake-key", "fake-account-id")
        
        # Mock client instance
        mock_client = AsyncMock()
        mock_client.create_task.return_value = "morgen-task-id-abc"
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        # Call tool
        result = await add_morgen_task(
            title="Morgen Tool Task",
            description="Created by tool",
            priority="Critical",
            db_path=self.db_path
        )

        self.assertIn("Successfully created Morgen task", result)
        self.assertIn("morgen-task-id-abc", result)

        # Check DB
        task = db.get_task("morgen-task-id-abc", db_path=self.db_path)
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "Morgen Tool Task")
        self.assertEqual(task["priority"], "Critical")

    @patch("enuclea.morgen_tool.load_morgen_credentials")
    @patch("enuclea.morgen_tool.MorgenClient")
    async def test_sync_scheduled_task(self, mock_client_cls, mock_credentials):
        mock_credentials.return_value = ("fake-key", "fake-account-id")
        
        # Add task to DB
        db.add_task(
            task_id="task-xyz",
            title="Task XYZ",
            priority="Medium",
            db_path=self.db_path
        )

        # Mock event list returning that task-xyz is scheduled
        mock_client = AsyncMock()
        async def mock_list_tasks(updated_after=None):
            if updated_after is None:
                return [{"id": "task-xyz", "title": "Task XYZ", "status": "needs-action"}]
            return []
        mock_client.list_tasks.side_effect = mock_list_tasks
        mock_client.list_events.return_value = [
            {
                "id": "evt-789",
                "title": "Scheduled Task XYZ",
                "start": "2026-06-25T10:00:00Z",
                "timeZone": "UTC",
                "morgen.so:metadata": {"taskId": "task-xyz"}
            }
        ]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        # Sync
        changes = await run_sync_and_get_changes(db_path=self.db_path)
        
        # Verify changes dict
        self.assertEqual(len(changes["scheduled"]), 1)
        self.assertEqual(changes["scheduled"][0]["id"], "task-xyz")
        self.assertEqual(changes["scheduled"][0]["scheduled_time"], "2026-06-25T10:00:00Z")

        # Verify DB is updated
        task = db.get_task("task-xyz", db_path=self.db_path)
        self.assertEqual(task["is_scheduled"], 1)
        self.assertEqual(task["scheduled_event_id"], "evt-789")
        self.assertEqual(task["scheduled_time"], "2026-06-25T10:00:00Z")

    @patch("enuclea.morgen_tool.load_morgen_credentials")
    @patch("enuclea.morgen_tool.MorgenClient")
    async def test_sync_unscheduled_task(self, mock_client_cls, mock_credentials):
        mock_credentials.return_value = ("fake-key", "fake-account-id")
        
        # Add task that was previously scheduled
        db.add_task(
            task_id="task-xyz",
            title="Task XYZ",
            priority="Medium",
            db_path=self.db_path
        )
        db.update_task_scheduling("task-xyz", True, "evt-789", "2026-06-25T10:00:00Z", db_path=self.db_path)

        # Mock event list returning NO events (task-xyz was removed/unscheduled)
        mock_client = AsyncMock()
        async def mock_list_tasks(updated_after=None):
            if updated_after is None:
                return [{"id": "task-xyz", "title": "Task XYZ", "status": "needs-action"}]
            return []
        mock_client.list_tasks.side_effect = mock_list_tasks
        mock_client.list_events.return_value = []
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        # Sync
        changes = await run_sync_and_get_changes(db_path=self.db_path)
        
        # Verify changes dict
        self.assertEqual(len(changes["unscheduled"]), 1)
        self.assertEqual(changes["unscheduled"][0]["id"], "task-xyz")

        # Verify DB is updated
        task = db.get_task("task-xyz", db_path=self.db_path)
        self.assertEqual(task["is_scheduled"], 0)
        self.assertIsNone(task["scheduled_event_id"])
        self.assertIsNone(task["scheduled_time"])

    @patch("enuclea.morgen_tool.load_morgen_credentials")
    @patch("enuclea.morgen_tool.MorgenClient")
    async def test_sync_completed_task(self, mock_client_cls, mock_credentials):
        mock_credentials.return_value = ("fake-key", "fake-account-id")
        
        # Add task to DB
        db.add_task(
            task_id="task-abc",
            title="Task ABC",
            priority="Medium",
            db_path=self.db_path
        )

        # Mock list_tasks returning that task-abc is completed
        mock_client = AsyncMock()
        async def mock_list_tasks(updated_after=None):
            if updated_after is None:
                return []
            return [{"id": "task-abc", "title": "Task ABC", "status": "completed"}]
        mock_client.list_tasks.side_effect = mock_list_tasks
        mock_client.list_events.return_value = []
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        # Sync
        changes = await run_sync_and_get_changes(db_path=self.db_path)
        
        # Verify changes dict
        self.assertEqual(len(changes["completed"]), 1)
        self.assertEqual(changes["completed"][0]["id"], "task-abc")

        # Verify DB is updated
        task = db.get_task("task-abc", db_path=self.db_path)
        self.assertEqual(task["status"], "completed")

    @patch("enuclea.morgen_tool.load_morgen_credentials")
    @patch("enuclea.morgen_tool.MorgenClient")
    async def test_sync_deleted_task(self, mock_client_cls, mock_credentials):
        mock_credentials.return_value = ("fake-key", "fake-account-id")
        
        # Add task to DB and mark as scheduled
        db.add_task(
            task_id="task-del",
            title="Task to Delete",
            priority="Low",
            db_path=self.db_path
        )
        db.update_task_scheduling("task-del", True, "evt-del", "2026-06-25T10:00:00Z", db_path=self.db_path)

        # Mock list_tasks returning nothing (task is not active and not recently updated -> deleted)
        mock_client = AsyncMock()
        mock_client.list_tasks.return_value = []
        mock_client.list_events.return_value = []
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        # Sync
        changes = await run_sync_and_get_changes(db_path=self.db_path)

        # Verify changes dict
        self.assertEqual(len(changes["unscheduled"]), 1)
        self.assertEqual(changes["unscheduled"][0]["id"], "task-del")

        # Verify DB is updated to deleted and scheduling cleared
        task = db.get_task("task-del", db_path=self.db_path)
        self.assertEqual(task["status"], "deleted")
        self.assertEqual(task["is_scheduled"], 0)
        self.assertIsNone(task["scheduled_event_id"])
        self.assertIsNone(task["scheduled_time"])

        # Check subsequent sync excludes this task completely (no further unscheduled changes)
        changes2 = await run_sync_and_get_changes(db_path=self.db_path)
        self.assertEqual(len(changes2["unscheduled"]), 0)

    @patch("enuclea.morgen_tool.load_morgen_credentials")
    @patch("enuclea.morgen_tool.MorgenClient")
    @patch("enuclea.atera_tool.load_atera_credentials")
    @patch("enuclea.atera_tool.AteraClient")
    async def test_sync_morgen_task_schedules_updates_atera_ticket(self, mock_atera_client_cls, mock_atera_creds, mock_morgen_client_cls, mock_morgen_creds):
        mock_morgen_creds.return_value = ("fake-mkey", "fake-macc")
        mock_atera_creds.return_value = ("fake-akey", "fake-aurl")

        # Mock Atera client
        mock_atera = AsyncMock()
        mock_atera_client_cls.return_value.__aenter__.return_value = mock_atera

        # Mock Morgen client
        mock_morgen = AsyncMock()
        mock_morgen_client_cls.return_value.__aenter__.return_value = mock_morgen

        # Add task to DB
        db.add_task(
            task_id="task-101",
            title="Scheduled Task 101",
            priority="Medium",
            db_path=self.db_path
        )
        # Associate task with Atera ticket 5001
        db.add_tracked_atera_item("ticket_5001", "ticket", 5001, "task-101", db_path=self.db_path)

        # Mock Morgen list_tasks and list_events
        async def mock_list_tasks(updated_after=None):
            if updated_after is None:
                return [{"id": "task-101", "title": "Scheduled Task 101", "status": "needs-action"}]
            return []
        mock_morgen.list_tasks.side_effect = mock_list_tasks
        mock_morgen.list_events.return_value = [
            {
                "id": "evt-101",
                "title": "Scheduled Task 101 Event",
                "start": "2026-06-25T10:00:00Z",
                "timeZone": "UTC",
                "morgen.so:metadata": {"taskId": "task-101"}
            }
        ]

        # Sync morgen tasks
        result = await sync_morgen_tasks(db_path=self.db_path)
        self.assertIn("Scheduled Task 101", result)

        # Verify comment was posted to Atera ticket 5001
        mock_atera.add_ticket_comment.assert_called_once_with(
            5001,
            "This task has been scheduled for work at: 2026-06-25 10:00 UTC.",
            is_internal=False
        )

        # Verify ticket was assigned to Engineering group 7
        mock_atera.update_ticket_fields.assert_called_once_with(
            5001,
            {"TechnicianGroupID": 7}
        )

if __name__ == "__main__":
    unittest.main()
