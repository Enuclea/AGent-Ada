import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import discord
import sys
from pathlib import Path
import asyncio

# Add AGent/discord to sys.path to import bot
sys.path.insert(0, str(Path(__file__).parent.parent / "discord"))
import bot

class TestPriorityQueue(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile
        self.test_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.test_dir.name) / "test_discord_queue.db"
        
        self.db_path_patcher = patch("bot_queue.DB_PATH", self.test_db_path)
        self.db_path_patcher.start()
        
        bot.bot_queue.init_db()
        bot.task_queue = asyncio.PriorityQueue()
        bot.task_counter = 0

    def tearDown(self):
        self.db_path_patcher.stop()
        self.test_dir.cleanup()

    def test_get_message_priority_admin_command(self):
        message = MagicMock()
        message.content = "!ada config 1234 roleplay"
        message.guild = MagicMock()
        
        # config is admin command -> priority 0
        self.assertEqual(bot.get_message_priority(message), 0)

    def test_get_message_priority_mod_command(self):
        message = MagicMock()
        message.content = "!ada assess"
        message.guild = MagicMock()
        
        # assess is mod command -> priority 1
        self.assertEqual(bot.get_message_priority(message), 1)

    def test_get_message_priority_roleplay(self):
        message = MagicMock()
        message.content = "Ada, tell me a story."
        message.guild = MagicMock()
        
        # Check that we fall back to 2 (roleplay)
        with patch("bot.bot_config.get_channel_config", return_value={"purpose": "roleplay"}):
            self.assertEqual(bot.get_message_priority(message), 2)

    async def test_queue_priority_ordering(self):
        # Enqueue different priority tasks in random order and check popping order
        mock_msg = MagicMock()
        mock_msg.channel.send = AsyncMock()
        mock_msg.channel.trigger_typing = AsyncMock()

        # Enqueue: Moderator (1), Roleplay (2), Admin (0)
        await bot.enqueue_task(1, "command", mock_msg, "mod-task")
        await bot.enqueue_task(2, "roleplay", mock_msg, "rp-task")
        await bot.enqueue_task(0, "command", mock_msg, "admin-task")

        # Pop from priority queue
        first = await bot.task_queue.get()
        second = await bot.task_queue.get()
        third = await bot.task_queue.get()

        # Check priorities (first element of tuple)
        self.assertEqual(first[0], 0)   # Admin first
        self.assertEqual(second[0], 1)  # Mod second
        self.assertEqual(third[0], 2)   # Roleplay third

        # Check task data names/types
        self.assertEqual(first[2]["prompt_text"], "admin-task")
        self.assertEqual(second[2]["prompt_text"], "mod-task")
        self.assertEqual(third[2]["prompt_text"], "rp-task")

    async def test_queue_worker_execution(self):
        # Test queue worker executing tasks sequentially
        mock_msg = MagicMock()
        mock_msg.channel.send = AsyncMock()
        mock_msg.channel.trigger_typing = AsyncMock()

        executed = []

        async def dummy_process_commands(msg):
            await asyncio.sleep(0.01)
            executed.append(msg.content)

        # Patch bot.process_commands to check execution
        with patch("bot.bot.process_commands", dummy_process_commands):
            # Create a task for queue_worker and run it in background
            worker_task = asyncio.create_task(bot.queue_worker())

            # Enqueue command tasks
            msg1 = MagicMock()
            msg1.content = "cmd1"
            msg1.channel.send = AsyncMock()
            msg1.channel.trigger_typing = AsyncMock()

            msg2 = MagicMock()
            msg2.content = "cmd2"
            msg2.channel.send = AsyncMock()
            msg2.channel.trigger_typing = AsyncMock()

            await bot.enqueue_task(0, "command", msg1)
            await bot.enqueue_task(0, "command", msg2)

            # Wait for execution to finish
            await bot.task_queue.join()

            # Cancel worker task
            worker_task.cancel()

            self.assertEqual(executed, ["cmd1", "cmd2"])

if __name__ == "__main__":
    unittest.main()
