import os
import pytest
pytestmark = [pytest.mark.slow, pytest.mark.integration]
import aiohttp
from unittest.mock import AsyncMock, patch, MagicMock
from agent.keyless import KeylessAgyAgent

@pytest.mark.anyio
async def test_roleplay_routing_and_failover():
    agent = KeylessAgyAgent(model="gemini-3.5-flash", roleplay=True)
    assert agent.roleplay is True

    with patch("asyncio.create_subprocess_exec") as mock_exec, \
         patch("aiohttp.ClientSession.post") as mock_post, \
         patch("agent.routes.byok.load_api_keys", lambda: None), \
         patch.dict("os.environ", {"GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": ""}):
        
        proc_fail = AsyncMock()
        proc_fail.returncode = 1
        proc_fail.communicate.return_value = (b"", b"Gemini rate limit exceeded")
        mock_exec.return_value = proc_fail
        
        mock_resp = MagicMock()
        mock_resp.status = 200
        
        async def mock_json():
            return {"response": "Roleplay gemma response"}
            
        mock_resp.json = mock_json
        
        # Mock standard context manager for session.post
        mock_post.return_value.__aenter__.return_value = mock_resp
        
        response = await agent.chat("Let's roleplay!")
        
        assert response.text == "Roleplay gemma response"
        assert mock_exec.call_count == 6
        mock_post.assert_called_once()
        
        args, kwargs = mock_post.call_args
        assert args[0] == "http://10.200.0.4:11434/api/generate"
        assert kwargs["json"]["model"] == "gemma4:12b"
        assert kwargs["json"]["stream"] is False

@pytest.mark.anyio
async def test_roleplay_bypass_grok():
    agent = KeylessAgyAgent(model="gemini-3.5-flash", roleplay=True)
    
    with patch("asyncio.create_subprocess_exec") as mock_exec, \
         patch("aiohttp.ClientSession.post") as mock_post, \
         patch("agent.keyless.get_grok_path") as mock_grok_path, \
         patch("agent.routes.byok.load_api_keys", lambda: None), \
         patch.dict("os.environ", {"GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": ""}):
        
        mock_grok_path.return_value = "/usr/bin/grok"
        
        proc_fail = AsyncMock()
        proc_fail.returncode = 1
        proc_fail.communicate.return_value = (b"", b"Gemini fail")
        mock_exec.return_value = proc_fail
        
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_post.return_value.__aenter__.return_value = mock_resp
        
        with pytest.raises(RuntimeError) as exc_info:
            await agent.chat("Let's roleplay!")
            
        assert "All models in priority failover chain failed" in str(exc_info.value)
        assert mock_exec.call_count == 2

def test_character_parsing():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "discord"))
    from bot import parse_character_message

    # Standard character message
    char, msg = parse_character_message("[Ashemmi] Ada... My office. Now.")
    assert char == "Ashemmi"
    assert msg == "Ada... My office. Now."

    # Character message with space after brackets
    char, msg = parse_character_message("[Syanna]    Is it too early for alcohol?")
    assert char == "Syanna"
    assert msg == "Is it too early for alcohol?"

    # Non-character message
    char, msg = parse_character_message("Hello Ada!")
    assert char is None
    assert msg == "Hello Ada!"

    # OOC variation: empty message or just brackets
    char, msg = parse_character_message("[Kumo]")
    assert char == "Kumo"
    assert msg == ""

@pytest.mark.anyio
async def test_linkshell_routing():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "discord"))
    import bot
    import bot_config
    
    # Mock bot configs and functions
    with patch("bot_config.load_config") as mock_load_config, \
         patch("bot.enqueue_task") as mock_enqueue, \
         patch("bot.is_user_moderator") as mock_is_mod, \
         patch("bot.is_user_admin") as mock_is_admin:
         
        mock_load_config.return_value = {
            "default_model": "gemini-3.5-flash",
            "channels": {
                "980931413316628581": {
                    "channel_name": "linkshell",
                    "purpose": "roleplay",
                    "allowed_roles": ["@everyone"],
                    "allowed_users": [],
                    "on_mention": True,
                    "prefix": None
                }
            }
        }
        
        mock_is_mod.return_value = False
        mock_is_admin.return_value = False
        
        # Mock message in linkshell channel (ID: 980931413316628581)
        guild = MagicMock()
        guild.id = 980680159961178123 # Phoenix
        
        channel = MagicMock()
        channel.id = 980931413316628581 # linkshell
        channel.name = "linkshell"
        
        # Test case 1: Message mentions Ada
        msg_addressed = MagicMock()
        msg_addressed.guild = guild
        msg_addressed.channel = channel
        msg_addressed.author.id = 111111
        msg_addressed.author.bot = False
        msg_addressed.author.roles = []
        msg_addressed.content = "[Syanna] Ada, did you see my crystal plate?"
        msg_addressed.mentions = []
        
        mock_enqueue.reset_mock()
        await bot.on_message(msg_addressed)
        mock_enqueue.assert_called_once()
        assert mock_enqueue.call_args[0][1] == "roleplay"
        
        # Test case 2: Message does NOT mention Ada (should be ignored, no ambient trigger)
        msg_ambient = MagicMock()
        msg_ambient.guild = guild
        msg_ambient.channel = channel
        msg_ambient.author.id = 111111
        msg_ambient.author.bot = False
        msg_ambient.author.roles = []
        msg_ambient.content = "[Syanna] It is too early for alcohol."
        msg_ambient.mentions = []
        
        mock_enqueue.reset_mock()
        await bot.on_message(msg_ambient)
        mock_enqueue.assert_not_called()

        # Test case 3: Message is on Enuclea server (should not roleplay)
        enuclea_guild = MagicMock()
        enuclea_guild.id = 1418504570170118184 # Enuclea
        msg_enuclea = MagicMock()
        msg_enuclea.guild = enuclea_guild
        msg_enuclea.channel = channel
        msg_enuclea.author.id = 111111
        msg_enuclea.author.bot = False
        msg_enuclea.author.roles = []
        msg_enuclea.content = "[Syanna] Ada, did you see my crystal plate?"
        msg_enuclea.mentions = []
        
        mock_enqueue.reset_mock()
        await bot.on_message(msg_enuclea)
        mock_enqueue.assert_not_called()

@pytest.mark.anyio
async def test_linkshell_familiarity_and_prefix():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "discord"))
    import bot
    
    # We want to mock bot_config.load_config, get_familiarity_level, check_agent_server_status, ClientSession.post, channel.history
    with patch("bot_config.load_config") as mock_load_config, \
         patch("bot.get_familiarity_level") as mock_fam, \
         patch("bot.check_agent_server_status") as mock_status, \
         patch("aiohttp.ClientSession.post") as mock_post, \
         patch("bot.import_agent_memory") as mock_import_memory:
         
        mock_load_config.return_value = {
            "default_model": "gemini-3.5-flash",
            "channels": {
                "980931413316628581": {
                    "channel_name": "linkshell",
                    "purpose": "roleplay",
                    "allowed_roles": ["@everyone"],
                    "allowed_users": [],
                    "on_mention": True,
                    "prefix": None
                }
            }
        }
        mock_status.return_value = True
        mock_import_memory.return_value = None
        
        # side_effect for get_familiarity_level:
        # If session_id is Bar and speaker is "Kumo", return "Stranger"
        # If session_id is Bar and speaker is "Syanna", return "Trusted Regular"
        def fam_side_effect(session_id, patron_name, author_id=None):
            if session_id == "discord-roleplay-1518087367465111594":
                if patron_name == "Kumo":
                    return "Stranger"
                elif patron_name == "Syanna":
                    return "Trusted Regular"
            return "Stranger"
        mock_fam.side_effect = fam_side_effect
        
        # Mock message
        guild = MagicMock()
        guild.id = 980680159961178123
        
        channel = MagicMock()
        channel.id = 980931413316628581
        channel.send = AsyncMock()
        channel.trigger_typing = AsyncMock()
        
        mock_history = AsyncMock()
        mock_history.__aiter__.return_value = []
        channel.history.return_value = mock_history
        
        msg = MagicMock()
        msg.guild = guild
        msg.channel = channel
        msg.author.id = 111111
        msg.author.display_name = "Kumo"
        msg.author.bot = False
        msg.content = "[Kumo] Ada, hello!"
        
        # Mock POST response for API chat call
        mock_resp = MagicMock()
        mock_resp.status = 200
        
        # Server Sent Events mock stream
        # Send chunks: "Hello linkshell"
        async def mock_iter():
            yield b'data: {"type": "chunk", "content": "Hello linkshell"}\n'
            yield b'data: "[DONE]"\n'
        
        # Set up resp content to support iteration
        mock_resp.content = mock_iter()
        
        mock_post.return_value.__aenter__.return_value = mock_resp
        
        # Run handle_roleplay_query
        await bot.handle_roleplay_query(msg)
        
        # Check if the prefix '[Ada] ' was added when sending the message
        channel.send.assert_called_once_with("[Ada] Hello linkshell")


