import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import os
import sqlite3
import tempfile
import asyncio
try:
    from enuclea import db
    from enuclea.atera_tool import (
        load_atera_credentials,
        AteraClient,
        analyze_atera_item,
        create_morgen_task_for_item,
        sync_atera_to_morgen,
        sync_completed_atera_tasks,
        AteraAnalysis
    )
    has_enuclea = True
except ImportError:
    has_enuclea = False

import pytest
if not has_enuclea:
    pytestmark = pytest.mark.skip(reason="enuclea private module not available")

class TestEnucleaAtera(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        db.init_db(self.db_path)

    def tearDown(self):
        os.close(self.db_fd)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_init_db_schema(self):
        # Verify tracked_atera_items table exists
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracked_atera_items'")
        table_exists = cursor.fetchone()
        self.assertIsNotNone(table_exists)
        conn.close()

    def test_database_helpers(self):
        # 1. Check initially not tracked
        self.assertFalse(db.is_atera_item_tracked("ticket_1", db_path=self.db_path))

        # 2. Add item
        db.add_tracked_atera_item("ticket_1", "ticket", 1, "morgen-1", db_path=self.db_path)
        self.assertTrue(db.is_atera_item_tracked("ticket_1", db_path=self.db_path))

        # 3. Get open items
        open_items = db.get_open_tracked_atera_items(db_path=self.db_path)
        self.assertEqual(len(open_items), 1)
        self.assertEqual(open_items[0]["id"], "ticket_1")
        self.assertEqual(open_items[0]["status"], "open")

        # 4. Close item
        db.close_tracked_atera_item("morgen-1", db_path=self.db_path)
        open_items_after = db.get_open_tracked_atera_items(db_path=self.db_path)
        self.assertEqual(len(open_items_after), 0)

    @patch.dict(os.environ, {"ATERA_API_KEY": "fake-key", "ATERA_BASE_URL": "https://app.atera.com/api/v3"})
    def test_load_credentials(self):
        key, url = load_atera_credentials()
        self.assertEqual(key, "fake-key")
        self.assertEqual(url, "https://app.atera.com/api/v3")

    @patch("enuclea.keyless.KeylessAgyAgent")
    async def test_analyze_atera_item_ticket(self, mock_agent_cls):
        mock_response = AsyncMock()
        mock_response.structured_output.return_value = {
            "observations": "User cannot login.",
            "suggestions": "Reset password in Active Directory."
        }
        mock_agent = AsyncMock()
        mock_agent.chat.return_value = mock_response
        mock_agent_cls.return_value.__aenter__ = AsyncMock(return_value=mock_agent)
        mock_agent_cls.return_value.__aexit__ = AsyncMock()

        ticket = {
            "TicketID": 101,
            "TicketTitle": "Cannot Login",
            "TicketDescription": "I forgot my password.",
            "CustomerName": "Acme Corp",
            "TicketPriority": "High"
        }
        analysis = await analyze_atera_item("ticket", ticket)
        self.assertEqual(analysis.observations, "User cannot login.")
        self.assertEqual(analysis.suggestions, "Reset password in Active Directory.")

    @patch("enuclea.morgen_tool.add_morgen_task")
    async def test_create_morgen_task_for_item(self, mock_add_task):
        mock_add_task.return_value = "Successfully created Morgen task '...' (ID: morgen-task-id-123) and registered it locally."
        
        ticket = {
            "TicketID": 101,
            "TicketTitle": "Cannot Login",
            "TicketDescription": "I forgot my password.",
            "CustomerName": "Acme Corp",
            "TicketPriority": "High"
        }
        analysis = AteraAnalysis(observations="Obs", suggestions="Sug")
        task_id = await create_morgen_task_for_item("ticket", ticket, analysis, db_path=self.db_path)
        self.assertEqual(task_id, "morgen-task-id-123")

    @patch("enuclea.atera_tool.load_atera_credentials")
    @patch("enuclea.atera_tool.AteraClient")
    @patch("enuclea.atera_tool.analyze_atera_item")
    @patch("enuclea.atera_tool.create_morgen_task_for_item")
    async def test_sync_atera_to_morgen(self, mock_create, mock_analyze, mock_client_cls, mock_creds):
        mock_creds.return_value = ("fake-key", "fake-url")
        mock_client = AsyncMock()
        mock_client.fetch_open_tickets.return_value = [
            {"TicketID": 101, "TicketTitle": "Cannot Login", "TicketStatus": "Open"}
        ]
        mock_client.fetch_alerts.return_value = [
            {"AlertID": 202, "AlertCategoryID": "DiskFull", "Severity": "Warning"}
        ]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_analyze.return_value = AteraAnalysis(observations="Obs", suggestions="Sug")
        mock_create.side_effect = ["m1", "m2"]

        result = await sync_atera_to_morgen(db_path=self.db_path)
        self.assertIn("Created task for Ticket #101", result)
        self.assertIn("Created task for Alert #202", result)

        # Check they are in DB
        self.assertTrue(db.is_atera_item_tracked("ticket_101", db_path=self.db_path))
        self.assertTrue(db.is_atera_item_tracked("alert_202", db_path=self.db_path))

    @patch("enuclea.atera_tool.load_atera_credentials")
    @patch("enuclea.atera_tool.AteraClient")
    @patch("enuclea.atera_tool.analyze_atera_item")
    @patch("enuclea.atera_tool.create_morgen_task_for_item")
    async def test_sync_atera_to_morgen_spam(self, mock_create, mock_analyze, mock_client_cls, mock_creds):
        mock_creds.return_value = ("fake-key", "fake-url")
        mock_client = AsyncMock()
        mock_client.fetch_open_tickets.return_value = [
            {"TicketID": 105, "TicketTitle": "Instagram follower request", "TicketStatus": "Open"}
        ]
        mock_client.fetch_alerts.return_value = []
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_analyze.return_value = AteraAnalysis(
            observations="This is spam.",
            suggestions="No action.",
            is_spam=True,
            spam_reason="Instagram feed notification"
        )

        result = await sync_atera_to_morgen(db_path=self.db_path)
        self.assertIn("Auto-closed spam Ticket #105", result)
        self.assertNotIn("Created task for Ticket #105", result)

        # Check comment added and status updated to Closed
        mock_client.add_ticket_comment.assert_called_once()
        comment_arg = mock_client.add_ticket_comment.call_args[0]
        self.assertEqual(comment_arg[0], 105)
        self.assertIn("Automatically identified as spam", comment_arg[1])
        self.assertIn("Instagram feed notification", comment_arg[1])

        mock_client.update_ticket_status.assert_called_once_with(105, "Closed")

        # Confirm tracked in DB
        self.assertTrue(db.is_atera_item_tracked("ticket_105", db_path=self.db_path))
        # Ensure Morgen task was NOT created
        mock_create.assert_not_called()

    @patch("enuclea.atera_tool.load_atera_credentials")
    @patch("enuclea.atera_tool.AteraClient")
    async def test_sync_completed_atera_tasks(self, mock_client_cls, mock_creds):
        mock_creds.return_value = ("fake-key", "fake-url")
        mock_client = AsyncMock()
        mock_client.fetch_open_tickets.return_value = [{"TicketID": 101}]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        # Add a tracked open item to tracking database
        db.add_tracked_atera_item("ticket_101", "ticket", 101, "morgen-task-1", db_path=self.db_path)

        # Initially task isn't closed in local DB
        open_items = db.get_open_tracked_atera_items(db_path=self.db_path)
        self.assertEqual(len(open_items), 1)

        # Case 1: morgen_tasks doesn't have it or it's not completed -> no change
        await sync_completed_atera_tasks(db_path=self.db_path)
        open_items = db.get_open_tracked_atera_items(db_path=self.db_path)
        self.assertEqual(len(open_items), 1)

        # Case 2: morgen_tasks marks it completed -> sync closes tracked item
        db.add_task("morgen-task-1", "Atera Ticket", db_path=self.db_path)
        db.update_task_status("morgen-task-1", "completed", db_path=self.db_path)

        await sync_completed_atera_tasks(db_path=self.db_path)
        open_items_after = db.get_open_tracked_atera_items(db_path=self.db_path)
        self.assertEqual(len(open_items_after), 0)

    @patch("enuclea.atera_tool.load_atera_credentials")
    @patch("enuclea.atera_tool.AteraClient")
    async def test_usb_alert_silencing(self, mock_client_cls, mock_creds):
        mock_creds.return_value = ("fake-key", "fake-url")
        mock_client = AsyncMock()
        
        mock_client.fetch_open_tickets.return_value = []
        mock_client.fetch_alerts.return_value = [
            {
                "AlertID": 999,
                "AlertCategoryID": "Disk",
                "Title": "Disk Usage(F:)",
                "AlertMessage": "The Disk Usage(F:) 98.00% is greater than threshold.",
                "DeviceName": "DESKTOP",
                "CustomerID": 13,
                "AgentId": 15
            }
        ]
        mock_client.create_ticket.return_value = 888
        mock_client.update_ticket_status.return_value = True
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        
        # Register F on DESKTOP as USB
        db.add_usb_drive("DESKTOP", "F", db_path=self.db_path)
        self.assertTrue(db.is_usb_drive("DESKTOP", "F", db_path=self.db_path))
        
        # First sync run: should create & resolve ticket, and silence the alert
        result = await sync_atera_to_morgen(db_path=self.db_path)
        self.assertIn("Processed full USB disk alert #999 on DESKTOP", result)
        
        mock_client.create_ticket.assert_called_once_with(
            "Full disk alert received on DESKTOP usb drive. Please replace or reduce usage.",
            "Disk F: on computer DESKTOP is full (referenced in Atera Alert #999). Action: please change full usb disk.",
            13,
            priority="Low"
        )
        mock_client.update_ticket_status.assert_called_once_with(888, "Resolved")
        
        # Alert should be silenced
        # Alert should be silenced
        self.assertTrue(db.is_alert_silenced("DESKTOP", "disk_F", db_path=self.db_path))
        self.assertTrue(db.is_atera_item_tracked("alert_999", db_path=self.db_path))

    @patch("enuclea.atera_tool.load_atera_credentials")
    @patch("enuclea.atera_tool.AteraClient")
    @patch("enuclea.atera_tool.analyze_atera_item")
    @patch("enuclea.atera_tool.create_morgen_task_for_item")
    async def test_availability_alert_initial_ingestion(self, mock_create, mock_analyze, mock_client_cls, mock_creds):
        mock_creds.return_value = ("fake-key", "fake-url")
        mock_client = AsyncMock()
        mock_client.fetch_open_tickets.return_value = []
        mock_client.fetch_alerts.return_value = [
            {
                "AlertID": 10001,
                "AlertCategoryID": "Availability",
                "DeviceName": "DESKTOP-1",
                "AgentId": 50,
                "CustomerID": 13
            }
        ]
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_analyze.return_value = AteraAnalysis(observations="Obs", suggestions="Sug")
        mock_create.return_value = "morgen-task-availability-1"

        result = await sync_atera_to_morgen(db_path=self.db_path)
        self.assertIn("Logged new Availability Alert #10001", result)

        # Check DB tracking
        check = db.get_availability_check(10001, db_path=self.db_path)
        self.assertIsNotNone(check)
        self.assertEqual(check["agent_id"], 50)
        self.assertEqual(check["device_name"], "DESKTOP-1")
        self.assertEqual(check["check_count"], 0)
        self.assertEqual(check["status"], "checking")
        self.assertEqual(check["morgen_task_id"], "morgen-task-availability-1")

    @patch("enuclea.atera_tool.load_atera_credentials")
    @patch("enuclea.atera_tool.AteraClient")
    async def test_availability_alert_circle_back_offline(self, mock_client_cls, mock_creds):
        mock_creds.return_value = ("fake-key", "fake-url")
        mock_client = AsyncMock()
        mock_client.get_agent.return_value = {"AgentID": 50, "Online": False, "CustomerID": 13}
        mock_client.fetch_alerts.return_value = [] # no new alerts
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        # Seed check in DB with check_count = 0
        db.add_availability_check(10001, 50, "DESKTOP-1", "morgen-task-availability-1", db_path=self.db_path)

        await sync_atera_to_morgen(db_path=self.db_path)

        # Verify count is incremented to 1
        check = db.get_availability_check(10001, db_path=self.db_path)
        self.assertEqual(check["check_count"], 1)
        self.assertEqual(check["status"], "checking")

    @patch("enuclea.atera_tool.load_atera_credentials")
    @patch("enuclea.atera_tool.AteraClient")
    @patch("enuclea.morgen_client.MorgenClient")
    async def test_availability_alert_online_healing(self, mock_morgen_cls, mock_client_cls, mock_creds):
        mock_creds.return_value = ("fake-key", "fake-url")
        mock_client = AsyncMock()
        mock_client.get_agent.return_value = {"AgentID": 50, "Online": True, "CustomerID": 13}
        mock_client.fetch_alerts.return_value = []
        mock_client.delete_alert.return_value = True
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_morgen = AsyncMock()
        mock_morgen_cls.return_value.__aenter__.return_value = mock_morgen

        # Seed check in DB with check_count = 1
        db.add_availability_check(10001, 50, "DESKTOP-1", "morgen-task-availability-1", db_path=self.db_path)
        db.update_availability_check(10001, 1, "checking", db_path=self.db_path)

        with patch.dict(os.environ, {"MORGEN_API_KEY": "mkey", "MORGEN_ACCOUNT_ID": "macc"}):
            await sync_atera_to_morgen(db_path=self.db_path)

        # Verify alert deletion and Morgen task closure
        mock_client.delete_alert.assert_called_once_with(10001)
        mock_morgen.close_task.assert_called_once_with("morgen-task-availability-1")

        # Verify check status is 'online'
        check = db.get_availability_check(10001, db_path=self.db_path)
        self.assertEqual(check["check_count"], 2)
        self.assertEqual(check["status"], "online")

    @patch("enuclea.atera_tool.load_atera_credentials")
    @patch("enuclea.atera_tool.AteraClient")
    @patch("enuclea.morgen_client.MorgenClient")
    async def test_availability_alert_offline_triage_escalation(self, mock_morgen_cls, mock_client_cls, mock_creds):
        mock_creds.return_value = ("fake-key", "fake-url")
        mock_client = AsyncMock()
        mock_client.get_agent.return_value = {"AgentID": 50, "Online": False, "CustomerID": 13}
        mock_client.fetch_alerts.return_value = []
        mock_client.fetch_open_tickets.return_value = []
        mock_client.create_ticket.return_value = 5555
        mock_client.add_ticket_comment.return_value = True
        mock_client.update_ticket_fields.return_value = True
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_morgen = AsyncMock()
        mock_morgen_cls.return_value.__aenter__.return_value = mock_morgen

        # Seed check in DB with check_count = 2 (so next check is 3)
        db.add_availability_check(10001, 50, "DESKTOP-1", "morgen-task-availability-1", db_path=self.db_path)
        db.update_availability_check(10001, 2, "checking", db_path=self.db_path)

        with patch.dict(os.environ, {"ATERA_TRIAGE_GROUP_ID": "7", "ATERA_ENGINEERING_GROUP_ID": "", "MORGEN_API_KEY": "mkey", "MORGEN_ACCOUNT_ID": "macc"}):
            await sync_atera_to_morgen(db_path=self.db_path)

        # Verify ticket was created
        mock_client.create_ticket.assert_called_once_with(
            title="System Offline: DESKTOP-1",
            description="The system DESKTOP-1 is presently offline (referenced in Atera Alert #10001).",
            customer_id=13,
            priority="Low"
        )
        
        # Verify public and internal comments were posted
        mock_client.add_ticket_comment.assert_any_call(5555, "The system DESKTOP-1 is presently offline.", is_internal=False)
        mock_client.add_ticket_comment.assert_any_call(5555, unittest.mock.ANY, is_internal=True)
        
        # Verify ticket was moved to triage/engineering group ID 7 as Pending
        mock_client.update_ticket_fields.assert_called_once_with(5555, {"TicketStatus": "Pending", "TechnicianGroupID": 7})

        # Verify check status is 'offline_ticketed'
        check = db.get_availability_check(10001, db_path=self.db_path)
        self.assertEqual(check["check_count"], 3)
        self.assertEqual(check["status"], "offline_ticketed")
        self.assertEqual(check["ticket_id"], 5555)

        # Verify Morgen task was updated/combined
        mock_morgen.update_task.assert_called_once_with(
            "morgen-task-availability-1",
            title="[Atera Ticket #5555] System Offline: DESKTOP-1",
            description=unittest.mock.ANY
        )

        # Verify ticket is tracked in DB under the same morgen_task_id
        self.assertTrue(db.is_atera_item_tracked("ticket_5555", db_path=self.db_path))

    @patch("enuclea.atera_tool.load_atera_credentials")
    @patch("enuclea.atera_tool.AteraClient")
    @patch("enuclea.atera_tool.analyze_atera_item")
    @patch("enuclea.morgen_tool.add_morgen_task")
    async def test_it_automation_first_ticket_rollup(self, mock_add_morgen, mock_analyze, mock_client_cls, mock_creds):
        mock_creds.return_value = ("fake-key", "fake-url")
        mock_client = AsyncMock()
        
        # Ingest first automation ticket
        mock_client.fetch_open_tickets.return_value = [
            {
                "TicketID": 3001,
                "TicketTitle": "IT Automation Task Feedback: Run Sync",
                "TicketDescription": "Sync completed successfully.",
                "CustomerName": "Enuclea",
                "TicketCreatedDate": "2026-06-21T08:00:00Z"
            }
        ]
        mock_client.fetch_alerts.return_value = []
        mock_client.add_ticket_comment.return_value = True
        mock_client.update_ticket_fields.return_value = True
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        
        mock_analyze.return_value = AteraAnalysis(observations="Obs", suggestions="Sug")
        mock_add_morgen.return_value = "Created task (ID: morgen-rollup-task-1)"

        with patch.dict(os.environ, {"ATERA_TRIAGE_GROUP_ID": "7", "ATERA_ENGINEERING_GROUP_ID": ""}):
            result = await sync_atera_to_morgen(db_path=self.db_path)
            
        self.assertIn("Designated Ticket #3001 as Master IT Automation Rollup", result)
        
        # Verify rollup mapping in DB
        rollup = db.get_daily_automation_rollup("2026-06-21", db_path=self.db_path)
        self.assertIsNotNone(rollup)
        self.assertEqual(rollup["master_ticket_id"], 3001)
        self.assertEqual(rollup["morgen_task_id"], "morgen-rollup-task-1")
        
        # Verify Morgen task was created with master title
        mock_add_morgen.assert_called_once_with("[Master Rollup] IT Automation Feedback - 2026-06-21", unittest.mock.ANY, "Low", db_path=self.db_path)
        
        # Verify comments and field update (route to group ID 7)
        mock_client.add_ticket_comment.assert_called_once_with(3001, unittest.mock.ANY, is_internal=True)
        mock_client.update_ticket_fields.assert_called_once_with(3001, {"TicketStatus": "Pending", "TechnicianGroupID": 7})

    @patch("enuclea.atera_tool.load_atera_credentials")
    @patch("enuclea.atera_tool.AteraClient")
    @patch("enuclea.morgen_client.MorgenClient")
    async def test_it_automation_subsequent_ticket_merge(self, mock_morgen_cls, mock_client_cls, mock_creds):
        mock_creds.return_value = ("fake-key", "fake-url")
        mock_client = AsyncMock()
        
        # Ingest subsequent child ticket
        mock_client.fetch_open_tickets.return_value = [
            {
                "TicketID": 3002,
                "TicketTitle": "IT Automation Task Feedback: Patch Install",
                "TicketDescription": "Patch installation failed.",
                "CustomerName": "Enuclea",
                "TicketCreatedDate": "2026-06-21T09:00:00Z"
            }
        ]
        mock_client.fetch_alerts.return_value = []
        mock_client.add_ticket_comment.return_value = True
        mock_client.update_ticket_status.return_value = True
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        
        mock_morgen = AsyncMock()
        mock_morgen_cls.return_value.__aenter__.return_value = mock_morgen

        # Seed master rollup and task details
        db.add_daily_automation_rollup("2026-06-21", 3001, "morgen-rollup-task-1", db_path=self.db_path)
        db.add_task("morgen-rollup-task-1", "[Master Rollup] IT Automation Feedback - 2026-06-21", db_path=self.db_path)
        
        # Seed master ticket tracking
        db.add_tracked_atera_item("ticket_3001", "ticket", 3001, "morgen-rollup-task-1", db_path=self.db_path)

        with patch.dict(os.environ, {"MORGEN_API_KEY": "mkey", "MORGEN_ACCOUNT_ID": "macc"}):
            result = await sync_atera_to_morgen(db_path=self.db_path)
            
        self.assertIn("Merged child IT Automation ticket #3002 into Master ticket #3001", result)
        
        # Verify child ticket was set to Merged
        mock_client.update_ticket_status.assert_called_once_with(3002, "Merged")
        
        # Verify comment on child and master ticket
        mock_client.add_ticket_comment.assert_any_call(3002, "This IT Automation task feedback has been merged into the master daily ticket #3001.", is_internal=True)
        mock_client.add_ticket_comment.assert_any_call(3001, unittest.mock.ANY, is_internal=True)
        
        # Verify Morgen task update was called with consolidated description
        mock_morgen.update_task.assert_called_once_with("morgen-rollup-task-1", description=unittest.mock.ANY)
        
        # Verify child ticket is tracked in DB
        self.assertTrue(db.is_atera_item_tracked("ticket_3002", db_path=self.db_path))

    @patch("enuclea.atera_tool.load_atera_credentials")
    @patch("enuclea.atera_tool.AteraClient")
    @patch("enuclea.atera_tool.analyze_atera_item")
    @patch("enuclea.atera_tool.create_morgen_task_for_item")
    async def test_new_ticket_telemetry_analysis_private_note(self, mock_create, mock_analyze, mock_client_cls, mock_creds):
        mock_creds.return_value = ("fake-key", "fake-url")
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        # Return a new untracked ticket with "DESKTOP" in title, CustomerID=13
        mock_client.fetch_open_tickets.return_value = [
            {
                "TicketID": 105,
                "TicketTitle": "System Offline: DESKTOP",
                "TicketDescription": "PC offline since Friday.",
                "CustomerID": 13,
                "CustomerName": "Plaster Magic",
                "TicketPriority": "Medium",
                "TicketStatus": "Open"
            }
        ]
        mock_client.fetch_alerts.return_value = []

        # Mock GET agents/customer/13 request
        mock_client._request.return_value = {
            "items": [
                {
                    "AgentID": 15,
                    "MachineName": "DESKTOP",
                    "AgentName": "DESKTOP",
                    "Online": False
                }
            ]
        }

        # Mock get_agent for AgentID 15
        mock_agent_info = {
            "AgentID": 15,
            "MachineName": "DESKTOP",
            "OS": "Microsoft Windows 10 Pro x64",
            "Online": False,
            "LastSeen": "2026-06-19T21:50:23Z",
            "LastRebootTime": "2026-06-19T21:48:36Z",
            "IpAddresses": ["192.168.40.154"],
            "HardwareDisks": [
                {"Drive": "C:", "Free": 722878, "Used": 218428, "Total": 941306},
                {"Drive": "F:", "Free": 0, "Used": 1907727, "Total": 1907727}
            ]
        }
        mock_client.get_agent.return_value = mock_agent_info

        # Mock Gemini analysis & Morgen task creation
        mock_analyze.return_value = AteraAnalysis(
            observations="DESKTOP is offline and disk F is full.",
            suggestions="Verify network connection and check disk F backup space."
        )
        mock_create.return_value = "morgen-task-105"

        with patch.dict(os.environ, {"MORGEN_API_KEY": "mkey", "MORGEN_ACCOUNT_ID": "macc"}):
            result = await sync_atera_to_morgen(db_path=self.db_path)

        # 1. Verify sync result
        self.assertIn("Created task for Ticket #105: 'System Offline: DESKTOP'", result)

        # 2. Verify agent info lookup occurred
        mock_client._request.assert_any_call("GET", "agents/customer/13")
        mock_client.get_agent.assert_called_once_with(15)

        # 3. Verify analyze_atera_item called with agent_info
        mock_analyze.assert_called_once_with("ticket", mock_client.fetch_open_tickets.return_value[0], agent_info=mock_agent_info)

        # 4. Verify private note comment was posted with telemetry & recommendations
        mock_client.add_ticket_comment.assert_called_once()
        args, kwargs = mock_client.add_ticket_comment.call_args
        t_id_arg, comment_text_arg = args[0], args[1]
        is_internal_arg = kwargs.get("is_internal") if "is_internal" in kwargs else args[2]

        self.assertEqual(t_id_arg, 105)
        self.assertEqual(is_internal_arg, True)
        self.assertIn("DESKTOP is offline and disk F is full.", comment_text_arg)
        self.assertIn("Verify network connection and check disk F backup space.", comment_text_arg)
        self.assertIn("Microsoft Windows 10 Pro x64", comment_text_arg)
        self.assertIn("Drive F:: 0 MB Free / 1907727 MB Total", comment_text_arg)

        # 5. Verify tracked item saved to DB
        self.assertTrue(db.is_atera_item_tracked("ticket_105", db_path=self.db_path))


if has_enuclea:
    from enuclea.api_broker import APIBroker

class TestAPIBroker(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        db.init_db(self.db_path)
        self.broker = APIBroker(db_path=self.db_path)
        # Fast fill rate for tests to avoid sleeping/waiting
        for service in self.broker.rate_limiters:
            self.broker.rate_limiters[service]["fill_rate"] = 1000.0
            self.broker.rate_limiters[service]["tokens"] = 1000.0

    def tearDown(self):
        os.close(self.db_fd)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    async def test_cache_hit_miss(self):
        call_count = 0
        async def mock_call():
            nonlocal call_count
            call_count += 1
            return {"data": call_count}

        # First call (GET) - Cache miss
        res1 = await self.broker.call(
            service="atera",
            endpoint="test_endpoint",
            method="GET",
            request_func=mock_call,
            params={"param1": "val1"},
            cache_ttl=5
        )
        self.assertEqual(res1, {"data": 1})
        self.assertEqual(call_count, 1)

        # Second call (GET) - Cache hit (should not increment call_count)
        res2 = await self.broker.call(
            service="atera",
            endpoint="test_endpoint",
            method="GET",
            request_func=mock_call,
            params={"param1": "val1"},
            cache_ttl=5
        )
        self.assertEqual(res2, {"data": 1})
        self.assertEqual(call_count, 1)

        # Check logs table in DB
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT service, endpoint, method, success, outcome_tags FROM api_call_logs")
        logs = cursor.fetchall()
        conn.close()
        
        self.assertEqual(len(logs), 2)
        # First log should have outcome_tags=None or not cache_hit
        self.assertNotEqual(logs[0][4], "cache_hit")
        # Second log should have outcome_tags="cache_hit"
        self.assertEqual(logs[1][4], "cache_hit")

    async def test_api_call_logs_failure(self):
        async def mock_fail_call():
            raise ValueError("API error occurs here")

        with self.assertRaises(ValueError):
            await self.broker.call(
                service="morgen",
                endpoint="fail_endpoint",
                method="POST",
                request_func=mock_fail_call
            )

        # Check that failure log was written
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT service, endpoint, success, error FROM api_call_logs")
        logs = cursor.fetchall()
        conn.close()

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0][0], "morgen")
        self.assertEqual(logs[0][1], "fail_endpoint")
        self.assertEqual(logs[0][2], 0) # success = 0
        self.assertIn("API error occurs here", logs[0][3])

    async def test_transient_retry_backoff(self):
        # We want to verify that the broker retries transient errors
        call_count = 0
        async def mock_retry_call():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                # Raise 503 Service Unavailable or network error
                raise ConnectionError("Transient network failure")
            return "success_val"

        # Mock asyncio.sleep to avoid waiting during test backoff
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            res = await self.broker.call(
                service="gmail",
                endpoint="retry_endpoint",
                method="GET",
                request_func=mock_retry_call,
                cache_ttl=0 # disable caching to isolate retry logic
            )
            self.assertEqual(res, "success_val")
            self.assertEqual(call_count, 3)
            # Sleep should have been called twice (once for backoff=1.0, once for backoff=2.0)
            self.assertEqual(mock_sleep.call_count, 2)
            mock_sleep.assert_has_calls([unittest.mock.call(1.0), unittest.mock.call(2.0)])

    async def test_timeout_reduces_chunk_size(self):
        call_count = 0
        requested_params = []
        
        async def mock_timeout_call():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Capture a snapshot of the current state of parameters
                requested_params.append(dict(test_params))
                # Simulate timeout
                raise asyncio.TimeoutError("Gateway timeout")
            else:
                requested_params.append(dict(test_params))
                return "recovered"

        test_params = {"page": 1, "itemsInPage": 100, "maxResults": 50}

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            res = await self.broker.call(
                service="atera",
                endpoint="tickets",
                method="GET",
                request_func=mock_timeout_call,
                params=test_params,
                cache_ttl=0
            )
            self.assertEqual(res, "recovered")
            self.assertEqual(call_count, 2)
            # The first attempt should have used the original params
            self.assertEqual(requested_params[0]["itemsInPage"], 100)
            self.assertEqual(requested_params[0]["maxResults"], 50)
            # The second attempt should have used the halved params
            self.assertEqual(requested_params[1]["itemsInPage"], 50)
            self.assertEqual(requested_params[1]["maxResults"], 25)
            # Verify in-place modification
            self.assertEqual(test_params["itemsInPage"], 50)
            self.assertEqual(test_params["maxResults"], 25)

    async def test_cache_capacity_and_fifo_eviction(self):
        # Manually populate cache to simulate 1000 items
        self.broker.cache = {}
        for i in range(1000):
            self.broker.cache[(f"svc_{i}", "ep", "GET", "", "")] = (asyncio.get_event_loop().time(), f"val_{i}")
        
        # Capture first key which should be evicted first
        first_key_expected_evicted = next(iter(self.broker.cache))
        
        async def mock_call():
            return "new_val"
            
        await self.broker.call(
            service="new_svc",
            endpoint="ep",
            method="GET",
            request_func=mock_call,
            cache_ttl=5
        )
        
        self.assertEqual(len(self.broker.cache), 1000)
        self.assertNotIn(first_key_expected_evicted, self.broker.cache)
        self.assertIn(("new_svc", "ep", "GET", "", ""), self.broker.cache)

    async def test_cache_expiration_cleanup(self):
        # Populate cache with one old entry (> 3600s) and one fresh entry
        now = asyncio.get_event_loop().time()
        self.broker.cache = {
            ("svc1", "ep", "GET", "", ""): (now - 4000, "val1"),
            ("svc2", "ep", "GET", "", ""): (now, "val2"),
        }
        
        async def mock_call():
            return "val3"
            
        await self.broker.call(
            service="svc3",
            endpoint="ep",
            method="GET",
            request_func=mock_call,
            cache_ttl=5
        )
        
        # Entry 1 should be cleaned up; Entry 2 and 3 should remain
        self.assertNotIn(("svc1", "ep", "GET", "", ""), self.broker.cache)
        self.assertIn(("svc2", "ep", "GET", "", ""), self.broker.cache)
        self.assertIn(("svc3", "ep", "GET", "", ""), self.broker.cache)


class TestKeylessAgyAgentRobustness(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        db.init_db(self.db_path)

    def tearDown(self):
        os.close(self.db_fd)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    @patch("enuclea.keyless.get_harness_path", return_value="/mock/bin/agy")
    @patch("enuclea.keyless.get_grok_path", return_value="/mock/bin/grok")
    @patch("asyncio.create_subprocess_exec")
    async def test_keyless_agent_retry_success(self, mock_exec, mock_grok, mock_harness):
        mock_proc_fail = MagicMock()
        mock_proc_fail.returncode = 1
        mock_proc_fail.communicate = AsyncMock(return_value=(b"", b"Transient model error"))

        mock_proc_success = MagicMock()
        mock_proc_success.returncode = 0
        mock_proc_success.communicate = AsyncMock(return_value=(b'{"observations": "ok"}', b""))

        mock_exec.side_effect = [mock_proc_fail, mock_proc_success]

        from enuclea.keyless import KeylessAgyAgent
        agent = KeylessAgyAgent(db_path=self.db_path)
        
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            res = await agent.chat("test prompt")
            self.assertEqual(res.text, '{"observations": "ok"}')
            self.assertEqual(mock_exec.call_count, 2)
            self.assertEqual(mock_sleep.call_count, 1)

    @patch("enuclea.keyless.get_harness_path", return_value="/mock/bin/agy")
    @patch("enuclea.keyless.get_grok_path", return_value="/mock/bin/grok")
    @patch("asyncio.create_subprocess_exec")
    async def test_keyless_agent_grok_fallback_success(self, mock_exec, mock_grok, mock_harness):
        mock_proc_fail = MagicMock()
        mock_proc_fail.returncode = 1
        mock_proc_fail.communicate = AsyncMock(return_value=(b"", b"Transient model error"))

        mock_proc_grok = MagicMock()
        mock_proc_grok.returncode = 0
        mock_proc_grok.communicate = AsyncMock(return_value=(b'{"observations": "grok_ok"}', b""))

        mock_exec.side_effect = [mock_proc_fail, mock_proc_fail, mock_proc_grok]

        from enuclea.keyless import KeylessAgyAgent
        agent = KeylessAgyAgent(db_path=self.db_path)
        
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            res = await agent.chat("test prompt")
            self.assertEqual(res.text, '{"observations": "grok_ok"}')
            self.assertEqual(mock_exec.call_count, 3)
            self.assertEqual(mock_sleep.call_count, 1)

    @patch("enuclea.keyless.get_harness_path", return_value="/mock/bin/agy")
    @patch("enuclea.keyless.get_grok_path", return_value="/mock/bin/grok")
    @patch("asyncio.create_subprocess_exec")
    async def test_keyless_agent_grok_rate_limit(self, mock_exec, mock_grok, mock_harness):
        mock_proc_fail = MagicMock()
        mock_proc_fail.returncode = 1
        mock_proc_fail.communicate = AsyncMock(return_value=(b"", b"Transient model error"))

        mock_exec.return_value = mock_proc_fail

        import json
        import time
        now = time.time()
        grok_calls = [now - 100] * 5
        db.set_metadata("grok_fallback_calls", json.dumps(grok_calls), db_path=self.db_path)

        from enuclea.keyless import KeylessAgyAgent
        agent = KeylessAgyAgent(db_path=self.db_path)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with self.assertRaises(RuntimeError) as ctx:
                await agent.chat("test prompt")
            self.assertIn("Grok fallback blocked by rate limits", str(ctx.exception))
            self.assertEqual(mock_exec.call_count, 2)

    @patch("enuclea.keyless.get_harness_path", return_value="/mock/bin/agy")
    @patch("enuclea.keyless.get_grok_path", return_value=None)
    @patch("asyncio.create_subprocess_exec")
    async def test_keyless_agent_grok_binary_missing(self, mock_exec, mock_grok, mock_harness):
        mock_proc_fail = MagicMock()
        mock_proc_fail.returncode = 1
        mock_proc_fail.communicate = AsyncMock(return_value=(b"", b"Transient model error"))

        mock_exec.return_value = mock_proc_fail

        from enuclea.keyless import KeylessAgyAgent
        agent = KeylessAgyAgent(db_path=self.db_path)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with self.assertRaises(RuntimeError) as ctx:
                await agent.chat("test prompt")
            self.assertIn("Grok binary not found", str(ctx.exception))
            self.assertEqual(mock_exec.call_count, 2)


class TestAteraClientJsonFailure(unittest.IsolatedAsyncioTestCase):
    @patch("enuclea.api_broker.get_shared_broker")
    async def test_client_request_json_parse_failure(self, mock_broker_cls):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.side_effect = Exception("Invalid JSON")
        mock_resp.text = AsyncMock(return_value="<html>Cloudflare Gateway Error</html>")

        mock_session = MagicMock()
        mock_session.request.return_value.__aenter__.return_value = mock_resp

        mock_broker = MagicMock()
        async def mock_call(service, endpoint, method, request_func, **kwargs):
            return await request_func()
        mock_broker.call.side_effect = mock_call
        mock_broker_cls.return_value = mock_broker

        from enuclea.atera_tool import AteraClient
        client = AteraClient("api-key", "http://fake-base")
        client.session = mock_session

        with self.assertRaises(Exception) as ctx:
            await client._request("GET", "tickets")
        self.assertIn("Failed to parse JSON response on 200/201", str(ctx.exception))
        self.assertIn("Cloudflare Gateway Error", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


