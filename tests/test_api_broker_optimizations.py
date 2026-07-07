import unittest
import asyncio
import sys
import os
from unittest.mock import MagicMock, AsyncMock, patch

# Add AGent codebase to path
sys.path.append("/home/dan/AGent")

from enuclea.api_broker import APIBroker
from enuclea.gmail_client import fetch_messages

class TestAPIBrokerOptimizations(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile
        from enuclea.db import init_db
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_enuclea.db")
        init_db(self.db_path)
        self.broker = APIBroker(db_path=self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    async def test_rate_limiter_concurrency_non_blocking(self):
        # Set up rate limiter with 0 tokens and slow fill rate
        self.broker.rate_limiters["atera"]["tokens"] = 0.0
        self.broker.rate_limiters["atera"]["fill_rate"] = 0.1
        self.broker.rate_limiters["atera"]["capacity"] = 1.0
        
        # Start acquire task in the background
        task = asyncio.create_task(self.broker._rate_limiter.acquire("atera"))
        
        # Yield to let the task enter acquire_token and start sleeping
        await asyncio.sleep(0.05)
        
        # Assert that the lock is NOT held while sleeping (non-blocking)
        lock = self.broker.rate_limiters["atera"]["lock"]
        self.assertFalse(lock.locked(), "Lock should be released during sleep")
        
        # Cancel the background task to cleanup
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @patch("enuclea.gmail_client.list_messages_sync")
    async def test_gmail_parameter_bind_reduction(self, mock_list_messages):
        # Mock list_messages_sync to raise timeout on first call, success on second
        call_count = 0
        captured_max_results = []
        
        def side_effect(service, query, max_results):
            nonlocal call_count
            call_count += 1
            captured_max_results.append(max_results)
            if call_count == 1:
                raise asyncio.TimeoutError("Timeout")
            return []

        mock_list_messages.side_effect = side_effect
        mock_service = MagicMock()

        # Mock asyncio.sleep to avoid waiting during test backoff
        with patch("asyncio.sleep", new_callable=AsyncMock):
            res = await fetch_messages(mock_service, "is:unread", max_results=50)
            
            self.assertEqual(res, [])
            self.assertEqual(call_count, 2)
            # The first call should use max_results=50
            self.assertEqual(captured_max_results[0], 50)
            # The second call should use reduced max_results=25
            self.assertEqual(captured_max_results[1], 25)

if __name__ == "__main__":
    unittest.main()
