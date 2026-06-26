import os
import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from agent.keyless import KeylessAgyAgent

@pytest.mark.anyio
async def test_direct_api_routing():
    # Test routing to direct API when API key is set
    agent = KeylessAgyAgent(model="gemini-3.5-flash", timeout=1.0)
    
    with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}), \
         patch.object(agent, "_call_direct_api", new_callable=AsyncMock) as mock_direct:
         
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
