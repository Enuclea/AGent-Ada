import os
import pytest
pytestmark = [pytest.mark.slow, pytest.mark.integration]
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from agent.keyless import KeylessAgyAgent

@pytest.mark.anyio
async def test_direct_api_routing():
    # Test routing to direct API when API key is set AND AGENT_USE_DIRECT_API=true
    # Direct API is now the last resort after all agy and grok attempts fail
    agent = KeylessAgyAgent(model="gemini-3.5-flash", timeout=1.0)
    
    with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key", "AGENT_USE_DIRECT_API": "true"}), \
         patch.object(agent, "_call_direct_api", new_callable=AsyncMock) as mock_direct, \
         patch("asyncio.create_subprocess_exec") as mock_exec, \
         patch("agent.keyless.get_grok_path") as mock_grok_path:
         
        # All subprocess calls fail so we fall through to direct API
        proc_fail = AsyncMock()
        proc_fail.returncode = 1
        proc_fail.communicate.return_value = (b"", b"Model fail")
        mock_exec.return_value = proc_fail
        
        # No grok available
        mock_grok_path.return_value = None
        
        mock_direct.return_value = "Direct response content"
        response = await agent.chat("hello direct api")
        
        assert response.text == "Direct response content"
        mock_direct.assert_called_once()

@pytest.mark.anyio
async def test_failover_sequence_success():
    # Test that if primary model fails, the failover sequence proceeds to the next model
    agent = KeylessAgyAgent(model="primary-fail-model", timeout=5.0)
    
    # Mock direct API returning None so we trigger subprocess routing
    with patch.object(agent, "_call_direct_api", new_callable=AsyncMock) as mock_direct, \
         patch("asyncio.create_subprocess_exec") as mock_exec, \
         patch.object(agent, "_get_newest_conversation_id") as mock_conv:
         
        mock_direct.return_value = None
        mock_conv.return_value = "conv-id"
        
        # We need two subprocess mocks:
        # 1st mock (primary-fail-model) returns returncode = 1 (error)
        # 2nd mock (gemini-3.5-flash) returns returncode = 0 and valid output
        proc_fail = AsyncMock()
        proc_fail.returncode = 1
        proc_fail.communicate.return_value = (b"", b"Model quota exceeded or API down")
        
        proc_success = AsyncMock()
        proc_success.returncode = 0
        proc_success.communicate.return_value = (b"Successful failover output", b"")
        
        mock_exec.side_effect = [proc_fail, proc_fail, proc_success] # max_retries = 2
        
        response = await agent.chat("trigger failover")
        assert response.text == "Successful failover output"
        assert mock_exec.call_count == 3  # 2 attempts for failed model, 1 attempt for fallback model

@pytest.mark.anyio
async def test_grok_final_fallback():
    # Test final fallback to Grok when all agy models fail
    agent = KeylessAgyAgent(model="failing-model", db_path="/tmp/test_grok.db", timeout=5.0)
    
    with patch.object(agent, "_call_direct_api", new_callable=AsyncMock) as mock_direct, \
         patch("asyncio.create_subprocess_exec") as mock_exec, \
         patch("agent.keyless.get_grok_path") as mock_grok_path, \
         patch("agent.keyless.check_and_record_grok_usage") as mock_record_grok:
         
        mock_direct.return_value = None
        mock_grok_path.return_value = "/usr/bin/grok"
        mock_record_grok.return_value = True
        
        def mock_exec_side_effect(*args, **kwargs):
            cmd = args
            proc = AsyncMock()
            if cmd and "grok" in str(cmd[0]):
                proc.returncode = 0
                proc.communicate.return_value = (b"Grok response fallback", b"")
            else:
                proc.returncode = 1
                proc.communicate.return_value = (b"", b"Model fail error")
            return proc

        mock_exec.side_effect = mock_exec_side_effect
        
        response = await agent.chat("everything is down")
        assert response.text == "Grok response fallback"


def test_get_harness_path_security():
    """Verify security boundaries of the agy binary path returned by get_harness_path."""
    from agent.keyless import get_harness_path
    from unittest.mock import patch
    
    # Bypass ANTIGRAVITY_HARNESS_PATH env var
    with patch.dict(os.environ, {}):
        if "ANTIGRAVITY_HARNESS_PATH" in os.environ:
            del os.environ["ANTIGRAVITY_HARNESS_PATH"]
            
        # Case 1: shutil.which returns a safe path (e.g. /usr/bin/agy)
        with patch("shutil.which", return_value="/usr/bin/agy"), \
             patch("os.path.exists", return_value=False), \
             patch("os.path.isfile", return_value=False), \
             patch("pathlib.Path.exists", return_value=True):
            res = get_harness_path()
            assert res == "/usr/bin/agy"
            
        # Case 2: shutil.which returns an unsafe path inside working directory (workspace)
        cwd = os.getcwd()
        unsafe_workspace_path = os.path.join(cwd, "agy")
        with patch("shutil.which", return_value=unsafe_workspace_path), \
             patch("os.path.exists", return_value=False), \
             patch("os.path.isfile", return_value=False), \
             patch("pathlib.Path.exists", return_value=False):
            res = get_harness_path()
            assert res is None
            
        # Case 3: shutil.which returns an unsafe path in an untrusted directory (e.g. /tmp/agy)
        with patch("shutil.which", return_value="/tmp/agy"), \
             patch("os.path.exists", return_value=False), \
             patch("os.path.isfile", return_value=False), \
             patch("pathlib.Path.exists", return_value=False):
            res = get_harness_path()
            assert res is None
            
        # Case 4: shutil.which returns a safe path under ~/.local/bin/agy
        local_bin_path = os.path.expanduser("~/.local/bin/agy")
        with patch("shutil.which", return_value=None), \
             patch("os.path.exists", return_value=False), \
             patch("os.path.isfile", return_value=False), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.is_file", return_value=True):
            res = get_harness_path()
            assert res == local_bin_path
