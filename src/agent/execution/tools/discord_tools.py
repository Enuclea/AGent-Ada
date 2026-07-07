import os
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
import discord
from dotenv import load_dotenv

async def post_to_discord(channel: str, message: str, file_path: Optional[str] = None) -> str:
    """Posts a text message and an optional file attachment to a Discord channel.
    
    Args:
        channel: The channel name (e.g. 'general') or channel ID string.
        message: The text content of the message.
        file_path: Optional absolute file path of a file to upload/attach.
        
    Returns:
        JSON string indicating success or failure.
    """
    import aiohttp
    
    url = "http://127.0.0.1:8090/api/discord/post"
    payload = {
        "channel": channel,
        "message": message,
        "file_path": file_path
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10.0)) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    res_json = await resp.json()
                    return json.dumps(res_json)
                else:
                    err_txt = await resp.text()
                    return json.dumps({"status": "failed", "error": f"HTTP {resp.status}: {err_txt}"})
    except Exception as e:
        return json.dumps({"status": "failed", "error": str(e)})

async def read_discord_channel(channel: str, limit: int = 10) -> str:
    """Reads the recent message history of a Discord channel for context.
    
    Args:
        channel: The channel name (e.g. 'general') or channel ID string.
        limit: Number of recent messages to retrieve (max 50, default 10).
        
    Returns:
        JSON string listing the messages or containing an error.
    """
    import aiohttp
    
    url = f"http://127.0.0.1:8090/api/discord/messages?channel={channel}&limit={limit}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10.0)) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    res_json = await resp.json()
                    return json.dumps(res_json)
                else:
                    err_txt = await resp.text()
                    return json.dumps({"status": "failed", "error": f"HTTP {resp.status}: {err_txt}"})
    except Exception as e:
        return json.dumps({"status": "failed", "error": str(e)})

async def list_discord_channels() -> str:
    """Lists all available text channels on the Discord guilds the bot is connected to.
    
    Returns:
        JSON string listing the channels or containing an error.
    """
    import aiohttp
    
    url = "http://127.0.0.1:8090/api/discord/channels"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10.0)) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    res_json = await resp.json()
                    return json.dumps(res_json)
                else:
                    err_txt = await resp.text()
                    return json.dumps({"status": "failed", "error": f"HTTP {resp.status}: {err_txt}"})
    except Exception as e:
        return json.dumps({"status": "failed", "error": str(e)})

async def backup_discord_channel(channel_id: str) -> str:
    """Backs up all messages from a given Discord channel to a text file.
    
    This tool is only available on the web-side dashboard and cannot be triggered from Discord.
    
    Args:
        channel_id: The ID of the Discord channel to back up (must be a numeric string or integer).
    """
    # 1. Validate channel_id to prevent any directory traversal or injection
    channel_id_str = str(channel_id).strip()
    if not channel_id_str.isdigit():
        return "Error: channel_id must be a numeric string or integer containing only digits."

    # 2. Determine file paths and apply strict path containment checks
    project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
    backup_dir = project_root / "discord" / "channel_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    backup_file_path = backup_dir / f"discord_channel_{channel_id_str}_backup.txt"
    
    # Resolve paths for absolute verification
    abs_backup_file = backup_file_path.resolve()
    abs_backup_dir = backup_dir.resolve()
    if not str(abs_backup_file).startswith(str(abs_backup_dir) + os.path.sep):
        return "Error: Directory traversal attempt detected."

    # 3. Load Discord Bot Token
    discord_env_path = project_root / "discord" / ".env"
    if discord_env_path.exists():
        load_dotenv(discord_env_path)
    else:
        load_dotenv(Path.home() / ".agent" / ".env")
        load_dotenv()

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        return "Error: DISCORD_BOT_TOKEN is not configured in the environment or .env file."

    # 4. Initialize Discord Client
    intents = discord.Intents.default()
    intents.message_content = True

    class BackupClient(discord.Client):
        def __init__(self, target_channel_id: int, file_path: Path, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.target_channel_id = target_channel_id
            self.file_path = file_path
            self.result_message = ""
            self.error = None

        async def on_ready(self):
            try:
                # Fetch channel
                channel = self.get_channel(self.target_channel_id)
                if not channel:
                    channel = await self.fetch_channel(self.target_channel_id)

                if not channel:
                    raise ValueError(f"Channel with ID {self.target_channel_id} not found.")

                if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel, discord.StageChannel)):
                    raise ValueError(f"Channel with ID {self.target_channel_id} is not a messageable text channel.")

                lines = []
                lines.append(f"--- Backup of Channel: {channel.name} (ID: {channel.id}) ---\n")
                lines.append(f"--- Generated at: {datetime.now(timezone.utc).isoformat()} UTC ---\n\n")

                messages_count = 0
                # Fetch messages recursively
                async for message in channel.history(limit=None, oldest_first=True):
                    timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
                    display_name = getattr(message.author, "display_name", "")
                    username = getattr(message.author, "name", "")
                    if display_name and username and display_name != username:
                        author = f"{display_name} ({username})"
                    else:
                        author = display_name or username or str(message.author)
                    content = message.content or ""
                    lines.append(f"[{timestamp}] {author}: {content}\n")

                    if message.attachments:
                        attachment_urls = ", ".join([att.url for att in message.attachments])
                        lines.append(f"  Attachments: {attachment_urls}\n")

                    if message.embeds:
                        for embed in message.embeds:
                            embed_parts = []
                            if embed.title:
                                embed_parts.append(f"Title: {embed.title}")
                            if embed.description:
                                embed_parts.append(f"Description: {embed.description}")
                            if embed.url:
                                embed_parts.append(f"URL: {embed.url}")
                            for field in embed.fields:
                                embed_parts.append(f"Field {field.name}: {field.value}")
                            if embed_parts:
                                lines.append(f"  Embed: {'; '.join(embed_parts)}\n")
                    messages_count += 1

                def _write_file():
                    with open(self.file_path, "w", encoding="utf-8") as f:
                        for line in lines:
                            f.write(line)

                await asyncio.to_thread(_write_file)

                self.result_message = f"Successfully backed up {messages_count} messages from channel #{channel.name} to {self.file_path}"
            except Exception as e:
                self.error = e
            finally:
                await self.close()

    client = BackupClient(target_channel_id=int(channel_id_str), file_path=abs_backup_file, intents=intents)
    try:
        await asyncio.wait_for(client.start(token), timeout=60.0)
    except asyncio.TimeoutError:
        await client.close()
        return "Error: Discord connection timed out after 60 seconds."
    except Exception as e:
        return f"Error: Failed to connect to Discord: {e}"

    if client.error:
        return f"Error: Failed to backup channel: {client.error}"

    return client.result_message
