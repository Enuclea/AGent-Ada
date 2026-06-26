import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from pathlib import Path
from agent.orchestrator import orchestration_service

def test_orchestration_service_init():
    assert orchestration_service.active_agents == {}

@pytest.mark.anyio
async def test_prepare_agent_config():
    with patch("agent.memory.load_memory") as mock_load, \
         patch("agent.memory.get_auto_rag_context", new_callable=AsyncMock) as mock_rag, \
         patch("agent.registry.tool_registry.get_registered_tools") as mock_tools:

        mock_load.return_value = {"facts": ["Orchestrator fact"], "key_value": {"user_name": "Dan"}}
        mock_rag.return_value = "[RAG snippet]"
        mock_tools.return_value = []

        config = await orchestration_service.prepare_agent_config(
            model="gemini-3.5-flash",
            session_id="test-session",
            disable_tools=True,
            roleplay=False
        )

        # Verify components
        assert "system_instructions" in config
        assert "capabilities" in config
        assert "tools" in config
        assert "policies" in config
        assert "hooks" in config

        # Verify instructions inclusion
        instructions = config["system_instructions"]
        assert "Orchestrator fact" in instructions
        assert "[RAG snippet]" in instructions
