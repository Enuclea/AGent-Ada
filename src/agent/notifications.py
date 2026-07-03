"""Shared Discord notification utilities for background agents and monitors.

Configuration is resolved via environment variables with filesystem fallback:
- DISCORD_BOT_TOKEN: The bot token (preferred)
- DISCORD_ENV_PATH: Path to .env file containing DISCORD_BOT_TOKEN= (fallback)
- DISCORD_CONFIG_PATH: Path to config.json with channel mappings (fallback)
"""

import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Optional, Dict, Any


def get_discord_config() -> Dict[str, Any]:
    """Load the Discord channel configuration from config.json.
    
    Resolution order:
    1. DISCORD_CONFIG_PATH environment variable.
    2. discord/config.json relative to project root (legacy fallback).

    Returns:
        A dictionary containing the parsed configuration, or empty dict if not found.
    """
    config_path_str: Optional[str] = os.environ.get("DISCORD_CONFIG_PATH")
    if config_path_str:
        config_path: Path = Path(config_path_str)
    else:
        config_path = Path(__file__).parent.parent.parent / "discord" / "config.json"
    
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[NOTIFICATIONS] Error loading Discord config: {e}")
    return {}


def get_bot_token() -> Optional[str]:
    """Load the Discord bot token.
    
    Resolution order:
    1. DISCORD_BOT_TOKEN environment variable (preferred).
    2. .env file at DISCORD_ENV_PATH (if set).
    3. discord/.env relative to project root (legacy fallback).

    Returns:
        The bot token string if found, or None.
    """
    # 1. Environment variable (preferred)
    token: Optional[str] = os.environ.get("DISCORD_BOT_TOKEN")
    if token:
        return token
    
    # 2. File-based fallback
    env_path_str: Optional[str] = os.environ.get("DISCORD_ENV_PATH")
    if env_path_str:
        env_path: Path = Path(env_path_str)
    else:
        env_path = Path(__file__).parent.parent.parent / "discord" / ".env"
    
    if env_path.exists():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("DISCORD_BOT_TOKEN="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception as e:
            print(f"[NOTIFICATIONS] Error loading Discord .env: {e}")
    return None


def send_discord_alert(text: str, channel_name: str = "control-room") -> bool:
    """Send a message to a Discord channel via the Bot API.
    
    Args:
        text: The message content (max 2000 chars, auto-truncated).
        channel_name: The target channel name to resolve from config.json.
        
    Returns:
        True if the message was sent successfully, False otherwise.
    """
    token: Optional[str] = get_bot_token()
    if not token:
        print("[NOTIFICATIONS] No Discord bot token found.")
        return False

    config: Dict[str, Any] = get_discord_config()
    # Default fallback channel ID
    channel_id: int = 1518056970538586272

    # Search for matching channel name in config
    for cid, info in config.get("channels", {}).items():
        if isinstance(info, dict) and info.get("channel_name") == channel_name:
            try:
                channel_id = int(cid)
                break
            except ValueError:
                pass

    url: str = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers: Dict[str, str] = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com/Rapptz/discord.py 2.3.2) Python/3.10"
    }

    # Discord messages are capped at 2000 chars, so truncate safely if needed
    if len(text) > 1950:
        text = text[:1950] + "\n... [truncated]"

    data: bytes = json.dumps({"content": text}).encode("utf-8")

    req: urllib.request.Request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.getcode() == 200
    except Exception as e:
        print(f"[NOTIFICATIONS] Failed to send Discord alert: {e}", file=sys.stderr)
        return False
