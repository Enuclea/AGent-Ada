import os
import sys
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from agent.routes.agy import AgyRoute

class BypassPytestCheck:
    def __enter__(self):
        self.pytest_module = sys.modules.get("pytest")
        if "pytest" in sys.modules:
            del sys.modules["pytest"]
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.pytest_module:
            sys.modules["pytest"] = self.pytest_module

@pytest.mark.anyio
async def test_agy_failover_quota_vs_congestion():
    """
    Verifies that:
    - Quota errors (containing rate limit, quota, etc.) retry the same candidate model.
    - Congestion/timeouts bypass retries and fail over to the next candidate model.
    """
    captured_commands = []
    
    # 1. Test Quota Error: Subprocess returns non-zero code and rate limit stderr
    async def mock_create_subprocess_exec(*args, **kwargs):
        captured_commands.append(args)
        
        proc = MagicMock()
        proc.returncode = 1
        
        async def mock_communicate():
            return b"", b"429: Rate limit / Quota exceeded"
            
        proc.communicate = mock_communicate
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_create_subprocess_exec), \
         patch("asyncio.sleep", return_value=None) as mock_sleep:
         
        route = AgyRoute()
        with BypassPytestCheck():
            await route.execute(prompt="Hello", model="gemini")
        
        # gemini calls should be 3 (1 initial + 2 retries), and then claude calls should be 3
        gemini_calls = [cmd for cmd in captured_commands if cmd[-1] == "gemini"]
        claude_calls = [cmd for cmd in captured_commands if cmd[-1] == "claude"]
        
        assert len(gemini_calls) == 3
        assert len(claude_calls) == 3
        assert mock_sleep.call_count == 4  # 2 retries for gemini, 2 retries for claude

    # 2. Test Congestion (TimeoutError): Should try gemini once, then immediately failover to claude once
    captured_commands.clear()
    
    async def mock_create_subprocess_exec_timeout(*args, **kwargs):
        captured_commands.append(args)
        
        proc = MagicMock()
        proc.returncode = 1
        
        async def mock_communicate():
            import asyncio
            raise asyncio.TimeoutError("timeout")
            
        proc.communicate = mock_communicate
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_create_subprocess_exec_timeout):
        route = AgyRoute()
        with BypassPytestCheck():
            await route.execute(prompt="Hello", model="gemini")
        
        # Since it timed out (congestion), it should NOT retry.
        # It should try gemini once, then claude once.
        gemini_calls = [cmd for cmd in captured_commands if cmd[-1] == "gemini"]
        claude_calls = [cmd for cmd in captured_commands if cmd[-1] == "claude"]
        
        assert len(gemini_calls) == 1
        assert len(claude_calls) == 1
        assert len(captured_commands) == 2
