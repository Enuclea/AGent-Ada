import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import unittest
import pytest
pytestmark = [pytest.mark.slow, pytest.mark.integration]
from unittest.mock import AsyncMock, MagicMock, patch
import os
import sqlite3
import tempfile
import asyncio
from datetime import datetime, timezone
try:
    from enuclea import db
    from enuclea.gmail_tool import sync_gmail_emails
    has_enuclea = True
except ImportError:
    has_enuclea = False

if not has_enuclea:
    pytestmark = pytest.mark.skip(reason="enuclea private module not available")

class TestEnucleaGmail(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.temp_token = Path(self.db_path).with_suffix(".token.json")
        self.temp_token.write_text("{}")
        db.init_db(self.db_path)

    def tearDown(self):
        os.close(self.db_fd)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass
        try:
            self.temp_token.unlink()
        except OSError:
            pass

    @patch("enuclea.gmail_tool.load_gmail_paths")
    @patch("enuclea.gmail_tool.get_gmail_service")
    @patch("enuclea.gmail_tool.fetch_messages")
    @patch("enuclea.gmail_tool.get_message_details")
    async def test_sync_gmail_initial_run(
        self, mock_get_details, mock_fetch_messages, mock_get_service, mock_load_paths
    ):
        mock_load_paths.return_value = (Path("/fake/creds.json"), self.temp_token)
        mock_get_service.return_value = MagicMock()
        
        # Mock latest email
        mock_fetch_messages.return_value = [{"id": "msg-123", "threadId": "thread-123"}]
        mock_get_details.return_value = {
            "id": "msg-123",
            "threadId": "thread-123",
            "subject": "Initial Email Subject",
            "sender": "sender@example.com",
            "date_str": "Sun, 21 Jun 2026 05:00:00 GMT",
            "internalDate": "1782034800000",  # ms timestamp
            "snippet": "Initial body snippet",
            "body": "This is the initial body text."
        }

        # Run sync
        result = await sync_gmail_emails(db_path=self.db_path)

        # Assertions
        self.assertIn("Initial check: Clock started at timestamp 1782034800000", result)
        self.assertIn("Initial Email Subject", result)
        
        # Verify stored timestamp in DB
        stored_ts = db.get_metadata("gmail_last_checked_timestamp", db_path=self.db_path)
        self.assertEqual(stored_ts, "1782034799999")

    @patch("enuclea.gmail_tool.load_gmail_paths")
    @patch("enuclea.gmail_tool.get_gmail_service")
    @patch("enuclea.gmail_tool.fetch_messages")
    async def test_sync_gmail_subsequent_run_no_emails(
        self, mock_fetch_messages, mock_get_service, mock_load_paths
    ):
        mock_load_paths.return_value = (Path("/fake/creds.json"), self.temp_token)
        mock_get_service.return_value = MagicMock()

        # Set initial timestamp in DB
        db.set_metadata("gmail_last_checked_timestamp", "1782034800000", db_path=self.db_path)

        # Mock no messages returned
        mock_fetch_messages.return_value = []

        # Run sync
        result = await sync_gmail_emails(db_path=self.db_path)

        # Assertions
        self.assertEqual(result, "No new emails detected since last check.")

    @patch("enuclea.gmail_tool.load_gmail_paths")
    @patch("enuclea.gmail_tool.get_gmail_service")
    @patch("enuclea.gmail_tool.fetch_messages")
    @patch("enuclea.gmail_tool.get_message_details")
    @patch("enuclea.gmail_tool.Agent")
    @patch("enuclea.keyless.KeylessAgyAgent")
    @patch("enuclea.gmail_tool.add_morgen_task")
    async def test_sync_gmail_subsequent_run_with_actionable_emails(
        self, mock_add_morgen, mock_keyless_agent_cls, mock_agent_cls, mock_get_details, mock_fetch_messages, mock_get_service, mock_load_paths
    ):
        mock_load_paths.return_value = (Path("/fake/creds.json"), self.temp_token)
        mock_get_service.return_value = MagicMock()

        # Set initial timestamp in DB
        db.set_metadata("gmail_last_checked_timestamp", "1782034800000", db_path=self.db_path)

        # Mock new message stubs
        mock_fetch_messages.return_value = [
            {"id": "msg-new-1", "threadId": "thread-new-1"},
            {"id": "msg-new-2", "threadId": "thread-new-2"}
        ]
        
        # Mock message details
        # Msg 1: older (skipped/already seen based on ts check if ts <= last_checked)
        # Msg 2: newer and actionable
        mock_get_details.side_effect = [
            {
                "id": "msg-new-1",
                "threadId": "thread-new-1",
                "subject": "Older Email",
                "sender": "friend@example.com",
                "date_str": "Sun, 21 Jun 2026 04:00:00 GMT",
                "internalDate": "1782031200000",  # less than last checked (1782034800000)
                "snippet": "Just a note",
                "body": "Note body"
            },
            {
                "id": "msg-new-2",
                "threadId": "thread-new-2",
                "subject": "Urgent Action Required",
                "sender": "boss@example.com",
                "date_str": "Sun, 21 Jun 2026 06:00:00 GMT",
                "internalDate": "1782038400000",  # greater than last checked
                "snippet": "Need report",
                "body": "Please finish report by tomorrow."
            }
        ]

        # Mock Agent and response.structured_output
        mock_response = AsyncMock()
        mock_response.structured_output.return_value = {
            "action_required": True,
            "importance_reason": "Urgent request from boss",
            "task_title": "Finish report for Boss",
            "task_description": "Request to finish report by tomorrow."
        }
        
        mock_agent = AsyncMock()
        mock_agent.chat.return_value = mock_response
        mock_agent_cls.return_value.__aenter__.return_value = mock_agent
        mock_keyless_agent_cls.return_value.__aenter__.return_value = mock_agent

        # Mock morgen task creation
        mock_add_morgen.return_value = "Successfully created Morgen task"

        # Run sync
        result = await sync_gmail_emails(db_path=self.db_path)

        # Assertions
        self.assertIn("Urgent Action Required", result)
        self.assertIn("[ACTIONABLE]", result)
        self.assertIn("Morgen Task Created: '[Email] Finish report for Boss (From: boss@example.com | Subj: Urgent Action Required)'", result)
        
        # Verify DB updated to the newer message timestamp
        stored_ts = db.get_metadata("gmail_last_checked_timestamp", db_path=self.db_path)
        self.assertEqual(stored_ts, "1782038400000")
        
        # Verify Morgen task creation parameters
        expected_desc = (
            "✉️ **Email Metadata**\n"
            "• **From:** boss@example.com\n"
            "• **Subject:** Urgent Action Required\n"
            "• **Date:** Sun, 21 Jun 2026 06:00:00 GMT\n"
            "• **Link:** [Open in Gmail](https://mail.google.com/mail/u/0/#all/thread-new-2)\n"
            "\n"
            "🤖 **AI Analysis / Suggested Action**\n"
            "Request to finish report by tomorrow.\n"
            "\n"
            "📝 **Email Body Snippet**\n"
            "Please finish report by tomorrow."
        )
        mock_add_morgen.assert_called_once_with(
            "[Email] Finish report for Boss (From: boss@example.com | Subj: Urgent Action Required)",
            expected_desc,
            priority="Medium",
            db_path=self.db_path
        )

    @patch("enuclea.gmail_tool.load_gmail_paths")
    @patch("enuclea.gmail_tool.get_gmail_service")
    @patch("enuclea.gmail_tool.fetch_messages")
    @patch("enuclea.gmail_tool.get_message_details")
    @patch("enuclea.gmail_tool.Agent")
    @patch("enuclea.keyless.KeylessAgyAgent")
    @patch("enuclea.gmail_tool.add_morgen_task")
    async def test_sync_gmail_with_thumbtack_lead(
        self, mock_add_morgen, mock_keyless_agent_cls, mock_agent_cls, mock_get_details, mock_fetch_messages, mock_get_service, mock_load_paths
    ):
        mock_load_paths.return_value = (Path("/fake/creds.json"), self.temp_token)
        mock_get_service.return_value = MagicMock()

        # Set initial timestamp in DB
        db.set_metadata("gmail_last_checked_timestamp", "1782034800000", db_path=self.db_path)

        # Mock a new Thumbtack message
        mock_fetch_messages.return_value = [{"id": "msg-new-t1", "threadId": "thread-new-t1"}]
        
        mock_get_details.side_effect = [
            {
                "id": "msg-new-t1",
                "threadId": "thread-new-t1",
                "subject": "New lead: Repair leaky pipe",
                "sender": "leads@thumbtack.com",
                "date_str": "Sun, 21 Jun 2026 06:00:00 GMT",
                "internalDate": "1782038400000",
                "snippet": "Need plumber to fix pipe",
                "body": "Customer has a leak in the kitchen sink pipe."
            }
        ]

        # Mock Agent output with Thumbtack lead analysis
        mock_response = AsyncMock()
        mock_response.structured_output.return_value = {
            "action_required": True,
            "importance_reason": "New lead from Thumbtack",
            "task_title": "Fix leaky pipe",
            "task_description": "Plumbing leak under kitchen sink.",
            "is_thumbtack_lead": True,
            "lead_grade": "A",
            "go_no_go": "Go",
            "technical_insights": "Use plumbers tape and inspect the valve seal."
        }
        
        mock_agent = AsyncMock()
        mock_agent.chat.return_value = mock_response
        mock_agent_cls.return_value.__aenter__.return_value = mock_agent
        mock_keyless_agent_cls.return_value.__aenter__.return_value = mock_agent

        # Mock morgen task creation
        mock_add_morgen.return_value = "Successfully created Morgen task"

        # Run sync
        result = await sync_gmail_emails(db_path=self.db_path)

        # Assertions
        self.assertIn("New lead: Repair leaky pipe", result)
        self.assertIn("[ACTIONABLE]", result)
        self.assertIn("Morgen Task Created", result)
        
        # Verify Morgen task creation parameters are formatted with the Thumbtack lead details
        expected_title = "[Thumbtack Lead - Go] Grade: A | New lead: Repair leaky pipe"
        expected_desc = (
            "✉️ **Email Metadata**\n"
            "• **From:** leads@thumbtack.com\n"
            "• **Subject:** New lead: Repair leaky pipe\n"
            "• **Date:** Sun, 21 Jun 2026 06:00:00 GMT\n"
            "• **Link:** [Open in Gmail](https://mail.google.com/mail/u/0/#all/thread-new-t1)\n"
            "\n"
            "🎯 **Thumbtack Lead Assessment**\n"
            "• **Grade/Rank:** A\n"
            "• **Go/No-Go Recommendation:** Go\n"
            "• **Technical Insights & Techniques:** Use plumbers tape and inspect the valve seal.\n"
            "\n"
            "🤖 **AI Analysis / Suggested Action**\n"
            "Plumbing leak under kitchen sink.\n"
            "\n"
            "📝 **Email Body Snippet**\n"
            "Customer has a leak in the kitchen sink pipe."
        )
        mock_add_morgen.assert_called_once_with(
            expected_title,
            expected_desc,
            priority="Medium",
            db_path=self.db_path
        )

    @patch("enuclea.gmail_tool.load_gmail_paths")
    @patch("enuclea.gmail_tool.get_gmail_service")
    @patch("enuclea.gmail_tool.fetch_messages")
    @patch("enuclea.gmail_tool.get_message_details")
    @patch("enuclea.gmail_tool.Agent")
    @patch("enuclea.keyless.KeylessAgyAgent")
    @patch("enuclea.gmail_tool.add_morgen_task")
    async def test_sync_gmail_with_yelp_lead(
        self, mock_add_morgen, mock_keyless_agent_cls, mock_agent_cls, mock_get_details, mock_fetch_messages, mock_get_service, mock_load_paths
    ):
        mock_load_paths.return_value = (Path("/fake/creds.json"), self.temp_token)
        mock_get_service.return_value = MagicMock()

        # Set initial timestamp in DB
        db.set_metadata("gmail_last_checked_timestamp", "1782034800000", db_path=self.db_path)

        # Mock a new Yelp message
        mock_fetch_messages.return_value = [{"id": "msg-new-y1", "threadId": "thread-new-y1"}]
        
        mock_get_details.side_effect = [
            {
                "id": "msg-new-y1",
                "threadId": "thread-new-y1",
                "subject": "Yelp lead: Fix broken door hinge",
                "sender": "no-reply@yelp.com",
                "date_str": "Sun, 21 Jun 2026 06:00:00 GMT",
                "internalDate": "1782038400000",
                "snippet": "Need help repairing door hinge",
                "body": "Customer has a squeaky and broken door hinge on their back patio door."
            }
        ]

        # Mock Agent output with Yelp lead analysis
        mock_response = AsyncMock()
        mock_response.structured_output.return_value = {
            "action_required": True,
            "importance_reason": "New lead from Yelp",
            "task_title": "Repair door hinge",
            "task_description": "Door hinge repair on back patio door.",
            "is_yelp_lead": True,
            "lead_grade": "B",
            "go_no_go": "Go",
            "technical_insights": "Replace the standard screws with 3-inch screws to grip the stud."
        }
        
        mock_agent = AsyncMock()
        mock_agent.chat.return_value = mock_response
        mock_agent_cls.return_value.__aenter__.return_value = mock_agent
        mock_keyless_agent_cls.return_value.__aenter__.return_value = mock_agent

        # Mock morgen task creation
        mock_add_morgen.return_value = "Successfully created Morgen task"

        # Run sync
        result = await sync_gmail_emails(db_path=self.db_path)

        # Assertions
        self.assertIn("Yelp lead: Fix broken door hinge", result)
        self.assertIn("[ACTIONABLE]", result)
        self.assertIn("Morgen Task Created", result)
        
        # Verify Morgen task creation parameters are formatted with the Yelp lead details
        expected_title = "[Yelp Lead - Go] Grade: B | Yelp lead: Fix broken door hinge"
        expected_desc = (
            "✉️ **Email Metadata**\n"
            "• **From:** no-reply@yelp.com\n"
            "• **Subject:** Yelp lead: Fix broken door hinge\n"
            "• **Date:** Sun, 21 Jun 2026 06:00:00 GMT\n"
            "• **Link:** [Open in Gmail](https://mail.google.com/mail/u/0/#all/thread-new-y1)\n"
            "\n"
            "🎯 **Yelp Lead Assessment**\n"
            "• **Grade/Rank:** B\n"
            "• **Go/No-Go Recommendation:** Go\n"
            "• **Technical Insights & Techniques:** Replace the standard screws with 3-inch screws to grip the stud.\n"
            "\n"
            "🤖 **AI Analysis / Suggested Action**\n"
            "Door hinge repair on back patio door.\n"
            "\n"
            "📝 **Email Body Snippet**\n"
            "Customer has a squeaky and broken door hinge on their back patio door."
        )
        mock_add_morgen.assert_called_once_with(
            expected_title,
            expected_desc,
            priority="Medium",
            db_path=self.db_path
        )

    @patch("enuclea.gmail_tool.load_gmail_paths")
    @patch("enuclea.gmail_tool.get_gmail_service")
    @patch("enuclea.gmail_tool.fetch_messages")
    @patch("enuclea.gmail_tool.get_message_details")
    @patch("enuclea.gmail_tool.Agent")
    @patch("enuclea.keyless.KeylessAgyAgent")
    @patch("enuclea.morgen_client.MorgenClient")
    @patch("enuclea.gmail_tool.load_morgen_credentials")
    @patch("enuclea.gmail_tool.add_morgen_task")
    async def test_sync_gmail_with_duplicate_lead_merged(
        self, mock_add_morgen, mock_load_morgen, mock_morgen_client_cls, mock_keyless_agent_cls, mock_agent_cls, mock_get_details, mock_fetch_messages, mock_get_service, mock_load_paths
    ):
        mock_load_morgen.return_value = ("dummy_key", "dummy_acc")
        mock_load_paths.return_value = (Path("/fake/creds.json"), self.temp_token)
        mock_get_service.return_value = MagicMock()

        # Add initial lead task to local DB
        db.add_task(
            task_id="existing-morgen-id-456",
            title="[Thumbtack Lead - Go] Grade: A | New lead! Ryan C.",
            description="Initial lead details",
            priority="Medium",
            db_path=self.db_path
        )

        # Set initial timestamp in DB
        db.set_metadata("gmail_last_checked_timestamp", "1782034800000", db_path=self.db_path)

        # Mock a reminder Thumbtack message for Ryan C.
        mock_fetch_messages.return_value = [{"id": "msg-new-t2", "threadId": "thread-new-t2"}]
        
        mock_get_details.side_effect = [
            {
                "id": "msg-new-t2",
                "threadId": "thread-new-t2",
                "subject": "Reminder: Ryan C. is waiting",
                "sender": "leads@thumbtack.com",
                "date_str": "Sun, 21 Jun 2026 07:00:00 GMT",
                "internalDate": "1782042000000",
                "snippet": "Ryan C. is waiting for response",
                "body": "Hi, Ryan C. is waiting for you to reply to their message."
            }
        ]

        # Mock Agent output with extracted lead name matching "Ryan C."
        mock_response = MagicMock()
        mock_response.structured_output = AsyncMock(return_value={
            "action_required": True,
            "importance_reason": "Reminder for existing lead",
            "task_title": "Ryan C. waiting",
            "task_description": "Follow up with Ryan C.",
            "is_thumbtack_lead": True,
            "lead_name": "Ryan C."
        })
        
        mock_agent = AsyncMock()
        mock_agent.chat.return_value = mock_response
        mock_agent_cls.return_value.__aenter__.return_value = mock_agent
        mock_keyless_agent_cls.return_value.__aenter__.return_value = mock_agent

        # Mock MorgenClient context manager & update_task
        mock_m_client = AsyncMock()
        mock_morgen_client_cls.return_value.__aenter__.return_value = mock_m_client

        # Run sync
        result = await sync_gmail_emails(db_path=self.db_path)

        # Assertions
        self.assertIn("[MERGED]", result)
        self.assertIn("Appended to existing lead task", result)

        # Morgen task creation should NOT be called
        mock_add_morgen.assert_not_called()

        # Morgen task update SHOULD be called
        mock_m_client.update_task.assert_called_once()
        call_args = mock_m_client.update_task.call_args
        self.assertEqual(call_args.args[0], "existing-morgen-id-456")
        self.assertIn("Update/Reply Received", call_args.kwargs["description"])
        self.assertIn("Reminder: Ryan C. is waiting", call_args.kwargs["description"])

        # Verify DB is updated
        task = db.get_task("existing-morgen-id-456", db_path=self.db_path)
        self.assertIn("Update/Reply Received", task["description"])
        self.assertIn("Ryan C. is waiting for you to reply to their message", task["description"])

if __name__ == "__main__":
    unittest.main()
