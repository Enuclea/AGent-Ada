import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.append(str(Path(__file__).resolve().parent.parent))
sys.path.append(str(Path(__file__).resolve().parent.parent / "discord"))

class TestClientSupportQuery(unittest.IsolatedAsyncioTestCase):
    @patch("bot_config.load_config")
    @patch("enuclea.atera_mapping.get_mapping")
    @patch("enuclea.atera_ticketing_service.create_client_ticket")
    @patch("enuclea.atera_ticketing_service.query_client_ticket_statuses")
    async def test_query_open_tickets(self, mock_query, mock_create, mock_get_mapping, mock_load_config):
        # 1. Setup mock mapping and return value
        mock_get_mapping.return_value = {
            "local_section": "Enuclea Labs",
            "atera_customer_id": 24,
            "atera_group_id": 7,
            "description": "Core labs"
        }
        mock_query.return_value = [
            {"TicketID": 12345, "TicketTitle": "Printer down", "TicketStatus": "Open", "TicketPriority": "Low", "CreatedDate": "2026-06-28"}
        ]
        
        # Import handle_client_support_query from the custom module
        try:
            from enuclea_commands import handle_client_support_query
        except ImportError:
            from custom_modules.enuclea_commands import handle_client_support_query
        
        # 2. Setup mock Discord message
        mock_message = MagicMock()
        mock_message.channel.name = "ticket-status"
        mock_message.channel.category.name = "Enuclea Labs"
        
        mock_placeholder = AsyncMock()
        
        # 3. Call query for "what are my open tickets"
        await handle_client_support_query(
            message=mock_message,
            prompt_text="what are my open tickets",
            placeholder=mock_placeholder
        )
        
        # 4. Verify it queried statuses rather than creating a ticket
        mock_query.assert_called_once_with("Enuclea Labs")
        mock_create.assert_not_called()
        mock_placeholder.edit.assert_called_once()
        edited_content = mock_placeholder.edit.call_args[1].get("content", "")
        self.assertIn("12345: Printer down", edited_content)

    @patch("bot_config.load_config")
    @patch("enuclea.atera_mapping.get_mapping")
    @patch("enuclea.atera_ticketing_service.create_client_ticket")
    @patch("enuclea.atera_ticketing_service.query_client_ticket_statuses")
    async def test_create_ticket_intent(self, mock_query, mock_create, mock_get_mapping, mock_load_config):
        # 1. Setup mock mapping and return value
        mock_get_mapping.return_value = {
            "local_section": "Enuclea Labs",
            "atera_customer_id": 24,
            "atera_group_id": 7,
            "description": "Core labs"
        }
        mock_create.return_value = 99999
        
        try:
            from enuclea_commands import handle_client_support_query
        except ImportError:
            from custom_modules.enuclea_commands import handle_client_support_query
        
        # 2. Setup mock Discord message
        mock_message = MagicMock()
        mock_message.channel.name = "ticket-status"
        mock_message.channel.category.name = "Enuclea Labs"
        
        mock_placeholder = AsyncMock()
        
        # 3. Call create for "open a support ticket: \"Printer Broken\" \"The printer in the main hall is not printing\""
        await handle_client_support_query(
            message=mock_message,
            prompt_text='open a support ticket: "Printer Broken" "The printer in the main hall is not printing"',
            placeholder=mock_placeholder
        )
        
        # 4. Verify it created the ticket
        mock_create.assert_called_once_with("Enuclea Labs", "Printer Broken", "The printer in the main hall is not printing", priority="Low")
        mock_query.assert_not_called()
        mock_placeholder.edit.assert_any_call(content="✅ Ticket #99999 created successfully!")
