"""Shared Discord notification utilities for background agents and monitors."""

import json
import sys
import urllib.request
from pathlib import Path
from typing import Optional


def get_discord_config() -> dict:
    """Loads the Discord channel configuration from the project's config.json."""
    config_path = Path(__file__).parent.parent.parent / "discord" / "config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[NOTIFICATIONS] Error loading Discord config: {e}")
    return {}


def get_bot_token() -> Optional[str]:
    """Loads the Discord bot token from the project's .env file."""
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
    """Sends a message to a Discord channel via the Bot API.
    
    Args:
        text: The message content (max 2000 chars, auto-truncated).
        channel_name: The target channel name to resolve from config.json.
        
    Returns:
        True if the message was sent successfully, False otherwise.
    """
    token = get_bot_token()
    if not token:
        print("[NOTIFICATIONS] No Discord bot token found.")
        return False

    config = get_discord_config()
    # Default fallback channel ID
    channel_id = 1518056970538586272

    for cid, info in config.get("channels", {}).items():
        if info.get("channel_name") == channel_name:
            try:
                channel_id = int(cid)
                break
            except ValueError:
                pass

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com/Rapptz/discord.py 2.3.2) Python/3.10"
    }

    # Discord messages are capped at 2000 chars, so truncate safely if needed
    if len(text) > 1950:
        text = text[:1950] + "\n... [truncated]"

    data = json.dumps({"content": text}).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.getcode() == 200
    except Exception as e:
        print(f"[NOTIFICATIONS] Failed to send Discord alert: {e}", file=sys.stderr)
        return False
