import sys
import os
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

# Ensure discord directory is in sys.path so we can import local modules
discord_dir = str(Path(__file__).resolve().parent.parent / "discord")
if discord_dir not in sys.path:
    sys.path.insert(0, discord_dir)

# Now import the bot components
import bot as discord_bot
import bot_config

@pytest.mark.anyio
async def test_log_moderation_alert_same_guild():
    # Setup mocks
    mock_guild = MagicMock()
    mock_guild.id = 111
    
    mock_author = MagicMock()
    mock_author.id = 123
    mock_author.mention = "<@123>"
    
    mock_channel = MagicMock()
    mock_channel.id = 456
    mock_channel.mention = "<#456>"
    
    mock_alerts_channel = AsyncMock()
    mock_alerts_channel.guild.id = 111  # Same guild ID
    
    # Mock bot.fetch_channel and guild.get_channel
    discord_bot.bot.fetch_channel = AsyncMock(return_value=mock_alerts_channel)
    mock_guild.get_channel = MagicMock(return_value=None)
    
    with patch("discord.Embed") as MockEmbed:
        await discord_bot.log_moderation_alert(
            guild=mock_guild,
            author=mock_author,
            channel=mock_channel,
            reason="Test Reason",
            content="Test Content"
        )
        
        # Verify it fetched the channel and sent the embed
        discord_bot.bot.fetch_channel.assert_called_once_with(1510531552768163970)
        mock_alerts_channel.send.assert_called_once()

@pytest.mark.anyio
async def test_log_moderation_alert_different_guild():
    # Setup mocks
    mock_guild = MagicMock()
    mock_guild.id = 111
    
    mock_author = MagicMock()
    mock_author.id = 123
    mock_author.mention = "<@123>"
    
    mock_channel = MagicMock()
    mock_channel.id = 456
    mock_channel.mention = "<#456>"
    
    mock_alerts_channel = AsyncMock()
    mock_alerts_channel.guild.id = 222  # Different guild ID!
    
    discord_bot.bot.fetch_channel = AsyncMock(return_value=mock_alerts_channel)
    mock_guild.get_channel = MagicMock(return_value=None)
    
    await discord_bot.log_moderation_alert(
        guild=mock_guild,
        author=mock_author,
        channel=mock_channel,
        reason="Test Reason",
        content="Test Content"
    )
    
    # Verify send was NOT called because of guild mismatch
    mock_alerts_channel.send.assert_not_called()

@pytest.mark.anyio
async def test_inspect_message_local_rules_link_protection_disabled():
    # Mock message
    mock_guild = MagicMock()
    mock_guild.id = 980680159961178123  # Phoenix Server
    
    mock_author = MagicMock()
    mock_author.id = 123
    mock_author.bot = False
    
    mock_channel = MagicMock()
    mock_channel.id = 456
    
    mock_message = MagicMock()
    mock_message.guild = mock_guild
    mock_message.author = mock_author
    mock_message.channel = mock_channel
    mock_message.content = "Check out http://untrusted.com"
    
    # Mock config where link protection is not enabled
    mock_config = {
        "link_protection_enabled_guilds": []
    }
    
    with patch("bot_config.load_config", return_value=mock_config), \
         patch("bot.is_user_admin", return_value=False), \
         patch("bot.is_user_moderator", return_value=False):
        
        result = await discord_bot.inspect_message_local_rules(mock_message)
        assert result is False  # Should not flag since link protection is disabled

@pytest.mark.anyio
async def test_inspect_message_local_rules_link_protection_enabled():
    # Mock message
    mock_guild = MagicMock()
    mock_guild.id = 980680159961178123  # Phoenix Server
    
    mock_author = MagicMock()
    mock_author.id = 123
    mock_author.bot = False
    
    mock_channel = MagicMock()
    mock_channel.id = 456
    
    mock_message = MagicMock()
    mock_message.guild = mock_guild
    mock_message.author = mock_author
    mock_message.channel = mock_channel
    mock_message.content = "Check out http://untrusted.com"
    
    # Mock config where link protection is enabled for Phoenix
    mock_config = {
        "link_protection_enabled_guilds": [980680159961178123]
    }
    
    # Mock log_moderation_alert so it doesn't make real API calls
    with patch("bot_config.load_config", return_value=mock_config), \
         patch("bot.is_user_admin", return_value=False), \
         patch("bot.is_user_moderator", return_value=False), \
         patch("bot.log_moderation_alert", new_callable=AsyncMock) as mock_alert:
         
        result = await discord_bot.inspect_message_local_rules(mock_message)
        assert result is True  # Should flag since link protection is enabled and domain untrusted
        mock_alert.assert_called_once()

def test_load_config_env_overrides():
    env_vars = {
        "DEFAULT_MODEL": "gemini-test",
        "ROLEPLAY_GUILD_IDS": "123,456",
        "BOSS_USER_IDS": "789",
        "MODERATION_CHANNEL_ID": "999",
        "ADMIN_USER_IDS": "admin1,admin2",
        "DISCORD_CHANNELS_CONFIG": '{"111": {"channel_name": "test-chan"}}'
    }
    mock_path = MagicMock()
    mock_path.exists.return_value = False
    with patch.dict(os.environ, env_vars), \
         patch("bot_config.CONFIG_FILE_PATH", mock_path):
        cfg = bot_config.load_config()
        assert cfg["default_model"] == "gemini-test"
        assert cfg["roleplay_guild_ids"] == [123, 456]
        assert cfg["boss_user_ids"] == [789]
        assert cfg["moderation_channel_id"] == 999
        assert cfg["admin_user_ids"] == ["admin1", "admin2"]
        assert cfg["channels"]["111"]["channel_name"] == "test-chan"
