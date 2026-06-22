import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import unittest
import asyncio
from agent.web import PriorityLock

class TestPriorityLock(unittest.IsolatedAsyncioTestCase):
    async def test_priority_ordering(self):
        lock = PriorityLock()
        order = []

        # We will acquire the lock in a task first to block others
        await lock.acquire(priority=1) # Holds lock

        async def worker(priority, name):
            await lock.acquire(priority)
            order.append(name)
            await asyncio.sleep(0.01)
            lock.release()

        # Enqueue other tasks with different priorities
        # Enqueue Priority 3, then 1, then 0, then 2
        t1 = asyncio.create_task(worker(3, "low-3"))
        await asyncio.sleep(0.001)
        t2 = asyncio.create_task(worker(1, "high-1"))
        await asyncio.sleep(0.001)
        t3 = asyncio.create_task(worker(0, "admin-0"))
        await asyncio.sleep(0.001)
        t4 = asyncio.create_task(worker(2, "med-2"))

        # Give tasks time to queue up in the lock
        await asyncio.sleep(0.02)

        # Release initial lock, this should trigger workers to run in priority order
        lock.release()

        # Wait for all workers to complete
        await asyncio.gather(t1, t2, t3, t4)

        # Expected order based on lowest priority integer:
        # admin-0 (Priority 0)
        # high-1 (Priority 1)
        # med-2 (Priority 2)
        # low-3 (Priority 3)
        self.assertEqual(order, ["admin-0", "high-1", "med-2", "low-3"])

    async def test_fifo_for_equal_priority(self):
        lock = PriorityLock()
        order = []

        await lock.acquire(priority=1)

        async def worker(priority, name):
            await lock.acquire(priority)
            order.append(name)
            await asyncio.sleep(0.01)
            lock.release()

        # Enqueue tasks with the same priority (order: a, b, c)
        t1 = asyncio.create_task(worker(1, "a"))
        await asyncio.sleep(0.001)
        t2 = asyncio.create_task(worker(1, "b"))
        await asyncio.sleep(0.001)
        t3 = asyncio.create_task(worker(1, "c"))

        await asyncio.sleep(0.02)
        lock.release()

        await asyncio.gather(t1, t2, t3)

        # FIFO order should be preserved
        self.assertEqual(order, ["a", "b", "c"])

if __name__ == "__main__":
    unittest.main()
