import os
import sys
import unittest
import asyncio
import pytest
pytestmark = [pytest.mark.slow, pytest.mark.integration]
import sqlite3
from pathlib import Path

# Adjust path so we can import from enuclea
sys.path.append(str(Path(__file__).resolve().parent.parent))

from enuclea import db
from enuclea.atera_mapping import init_mapping_table, add_mapping, get_mapping, remove_mapping
from enuclea.atera_ticketing_service import create_client_ticket, query_client_ticket_statuses
from enuclea.atera_onboarding import validate_client_credentials, create_new_contact
from enuclea.atera_tool import AteraClient, load_atera_credentials

class TestAteraIntegration(unittest.IsolatedAsyncioTestCase):
    
    @classmethod
    def setUpClass(cls):
        # Ensure database is initialized
        db.init_db()
        init_mapping_table()

    async def asyncSetUp(self):
        # We use a test division for our local mapping checks
        self.test_section = "Integration Test Client Division"
        self.test_customer_id = 25 # Antigravity Test Client
        self.test_group_id = 7 # Triage
        
        try:
            api_key, base_url = load_atera_credentials()
            async with AteraClient(api_key, base_url) as client:
                contacts_data = await client._request("GET", "contacts", params={"page": 1, "itemsInPage": 50})
                items = contacts_data.get("items", []) if contacts_data else []
                for c in items:
                    c_id = c.get("CustomerID")
                    if c_id and not c.get("Archived", False):
                        self.test_customer_id = c_id
                        break
        except Exception:
            pass
            
        add_mapping(self.test_section, self.test_customer_id, self.test_group_id, "Temporary integration test mapping")

    async def asyncTearDown(self):
        remove_mapping(self.test_section)

    async def test_1_mapping_correctness(self):
        """Verifies that the data mapping layer resolves local sections correctly."""
        mapping = get_mapping(self.test_section)
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping["atera_customer_id"], self.test_customer_id)
        self.assertEqual(mapping["atera_group_id"], self.test_group_id)

    async def test_2_onboarding_validation_and_registration(self):
        """Verifies onboarding wizard validation and contact creation workflows."""
        # Query sample contact from Atera dynamically to ensure we test with a valid pair
        api_key, base_url = load_atera_credentials()
        sample_email = None
        sample_customer_name = None
        
        async with AteraClient(api_key, base_url) as client:
            contacts_data = await client._request("GET", "contacts", params={"page": 1, "itemsInPage": 10})
            contacts = contacts_data.get("items", []) if contacts_data else []
            if contacts:
                for c in contacts:
                    cust_id = c.get("CustomerID")
                    email = c.get("Email")
                    if cust_id and email:
                        cust_data = await client._request("GET", f"customers/{cust_id}")
                        if cust_data and cust_data.get("CustomerName"):
                            sample_email = email
                            sample_customer_name = cust_data.get("CustomerName")
                            break

        if not sample_email:
            self.skipTest("No active Atera contacts/customers found for dynamic validation testing.")

        # Test valid validation
        res_valid = await validate_client_credentials(sample_email, sample_customer_name)
        self.assertEqual(res_valid["status"], "success")

        # Test invalid organization
        res_invalid_org = await validate_client_credentials(sample_email, "Nonexistent Org Limited")
        self.assertEqual(res_invalid_org["status"], "error")
        self.assertIn("was not found", res_invalid_org["message"])

        # Test unregistered email
        import time
        temp_email = f"temp_integration_user_{int(time.time())}@example.com"
        res_unreg = await validate_client_credentials(temp_email, sample_customer_name)
        self.assertEqual(res_unreg["status"], "create_contact")

        # Test new contact creation
        new_contact_id = await create_new_contact(res_unreg["customer_id"], temp_email, "Integration Test User")
        self.assertIsNotNone(new_contact_id)

        # Allow eventual consistency/indexing delay on Atera API with a retry loop
        res_after = None
        for attempt in range(6):
            await asyncio.sleep(3)
            res_after = await validate_client_credentials(temp_email, sample_customer_name)
            if res_after.get("status") == "success":
                break
        self.assertIsNotNone(res_after)
        self.assertEqual(res_after.get("status"), "success")

    async def test_3_ticketing_and_broker_logging(self):
        """Verifies ticket creation, routing, status query, and API broker logging."""
        title = "Integration Test Ticket Title"
        description = "This is an automated test ticket raised by the integration test suite."
        
        # Create ticket
        ticket_id = await create_client_ticket(self.test_section, title, description, priority="Low")
        self.assertIsNotNone(ticket_id)

        try:
            # Query client tickets with retries/sleep to allow eventual consistency
            tickets = []
            ticket_ids = []
            for attempt in range(4):
                tickets = await query_client_ticket_statuses(self.test_section)
                if tickets:
                    ticket_ids = [t["TicketID"] for t in tickets]
                    if int(ticket_id) in [int(tid) for tid in ticket_ids]:
                        break
                await asyncio.sleep(2)
            
            self.assertTrue(len(tickets) > 0)
            self.assertIn(int(ticket_id), [int(tid) for tid in ticket_ids])

            # Verify the API broker successfully logged the call details in the SQLite logs
            with db.get_db_connection() as conn:
                conn.row_factory = sqlite3.Row
                logs = conn.execute(
                    "SELECT service, endpoint, method, success FROM api_call_logs WHERE service = 'atera' ORDER BY id DESC LIMIT 5"
                ).fetchall()
                
                # Check that we have logged Atera API calls
                self.assertTrue(len(logs) > 0)
                services = [log_row["service"] for log_row in logs]
                self.assertIn("atera", services)
        finally:
            api_key, base_url = load_atera_credentials()
            async with AteraClient(api_key, base_url) as client:
                await client.update_ticket_status(ticket_id, "Closed")

if __name__ == "__main__":
    unittest.main()
