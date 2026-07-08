import asyncio
import os
import sys
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

# Ensure project root and src directory are in sys.path
_root = str(Path(__file__).resolve().parent.parent)
_src = str(Path(__file__).resolve().parent.parent / "src")
if _root not in sys.path:
    sys.path.append(_root)
if _src not in sys.path:
    sys.path.append(_src)

import discord
from discord.ext import commands, tasks
import aiohttp

import bot_config
import bot_queue

AGENT_API_BASE = bot_config.get_agent_api_base()

def get_api_headers(method: str, path: str, query: str = "", json_data: Optional[dict] = None) -> dict:
    """Generates authentication and content-type headers for an API request."""
    import json
    body_bytes = b""
    if json_data is not None:
        body_bytes = json.dumps(json_data).encode("utf-8")
    headers = bot_config.get_auth_headers(method, path, query, body_bytes)
    if json_data is not None:
        headers["Content-Type"] = "application/json"
    return headers

ROLEPLAY_GUILD_IDS = bot_config.get_roleplay_guild_ids()
BOSS_USER_IDS = bot_config.get_boss_user_ids()
MODERATION_CHANNEL_ID = bot_config.get_moderation_channel_id()
THUMBTACK_CHANNEL_ID = bot_config.get_thumbtack_channel_id()
BAR_CHANNEL_ID = bot_config.get_bar_channel_id()
LINKSHELL_CHANNEL_ID = bot_config.get_linkshell_channel_id()
AROUND_HOUSE_CHANNEL_ID = bot_config.get_around_house_channel_id()

import random

def prune_log_file(file_path: Path, max_lines: int = 2000, force: bool = False):
    try:
        if file_path.exists():
            if force or file_path.stat().st_size > 2 * 1024 * 1024:  # Bypassed if force=True
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                if len(lines) > max_lines:
                    pruned_lines = lines[-max_lines:]
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.writelines(pruned_lines)
    except Exception as e:
        print(f"Error pruning log file {file_path.name}: {e}")

def log_received_message(message: discord.Message, trigger_prompt: Optional[str] = None):
    try:
        log_file = Path(__file__).parent / "discord_received.log"
        timestamp = datetime.now().isoformat()
        guild_info = f"{message.guild.name} (ID: {message.guild.id})" if message.guild else "DM"
        channel_info = f"{message.channel.name} (ID: {message.channel.id})" if hasattr(message.channel, "name") else f"ID: {message.channel.id}"
        author_info = f"{message.author.name} (ID: {message.author.id})"
        if message.author.bot:
            author_info += " [BOT]"
            
        mentions_info = ", ".join([f"{u.name} ({u.id})" for u in message.mentions]) or "None"
        
        log_entry = (
            f"=== {timestamp} ===\n"
            f"From: {author_info}\n"
            f"Server: {guild_info}\n"
            f"Channel: {channel_info}\n"
            f"Mentions: {mentions_info}\n"
            f"Content: {message.content!r}\n"
        )
        if trigger_prompt is not None:
            log_entry += f"Action: Processed Agent Hook Query -> Prompt: {trigger_prompt!r}\n"
        log_entry += "======================\n\n"
        
        # Write to overall log
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(log_entry)
            
        # Prune overall log occasionally
        if random.random() < 0.02:
            prune_log_file(log_file, max_lines=2000)

        # Write to channel-specific log if it is monitored or explicitly linkshell/around-the-house
        if message.channel:
            channel_id_str = str(message.channel.id)
            chan_cfg = bot_config.get_channel_config(channel_id_str)
            # Linkshell and Around-the-house
            is_special_unconfigured = message.channel.id in [LINKSHELL_CHANNEL_ID, AROUND_HOUSE_CHANNEL_ID]
            if chan_cfg or is_special_unconfigured:
                chan_log_file = Path(__file__).parent / f"discord_channel_{channel_id_str}.log"
                with open(chan_log_file, "a", encoding="utf-8") as f:
                    f.write(log_entry)
                # Prune channel log occasionally with greater line depth
                if random.random() < 0.02:
                    prune_log_file(chan_log_file, max_lines=5000)
    except Exception as e:
        print(f"Error writing to logs: {e}")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

def load_custom_modules(bot):
    """Dynamically loads custom Discord command modules and task handlers from the custom_modules folder."""
    import importlib.util
    import sys
    from pathlib import Path

    custom_dir = Path(__file__).resolve().parent / "custom_modules"
    if not custom_dir.exists() or not custom_dir.is_dir():
        return

    # Add to sys.path
    if str(custom_dir) not in sys.path:
        sys.path.append(str(custom_dir))

    for item in custom_dir.iterdir():
        if item.is_file() and item.name.endswith(".py") and not item.name.startswith("_"):
            try:
                module_name = f"discord.custom_modules.{item.stem}"
                spec = importlib.util.spec_from_file_location(module_name, item)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                # Call setup function if it exists
                if hasattr(module, "setup"):
                    module.setup(bot)
                print(f"[Custom Module] Loaded: {item.name}")
            except Exception as e:
                print(f"[Custom Module] Failed to load {item.name}: {e}")

bot = commands.Bot(command_prefix="!ada ", intents=intents)
bot.custom_task_handlers = {}
load_custom_modules(bot)

# Cooldowns and quiet mode configurations for roleplay channels
roleplay_cooldowns = {}
ROLEPLAY_COOLDOWN_SECONDS = 4.0
quiet_channels = set()
roleplay_msg_counters = {}
roleplay_summary_timestamps = {}
ROLEPLAY_SUMMARY_COOLDOWN_SECONDS = 900.0  # 15 minutes
roleplay_ambient_counters = {}
roleplay_ambient_thresholds = {}

# Brokering queue and priority constants
PRIORITY_ADMIN = 0
PRIORITY_MODERATOR = 1
PRIORITY_ROLEPLAY = 2

task_queue = asyncio.PriorityQueue()
task_counter = 0
session_queues = {}
active_session_workers = set()

# Active session cache per channel ID to guarantee consistent context
def get_channel_session_id(channel_id: int) -> str:
    return f"discord-session-{channel_id}"

def is_user_admin(user_id: int) -> bool:
    if hasattr(bot, "owner_id") and user_id == bot.owner_id:
        return True
    if user_id == 405566743415750656:
        return True
    try:
        config = bot_config.load_config()
    except Exception:
        return False
    extra_admins = config.get("admin_user_ids", [])
    for aid in extra_admins:
        try:
            if int(aid) == user_id:
                return True
        except (ValueError, TypeError):
            pass
    return False

def is_user_moderator(user_id: int, guild_id: Optional[int] = None) -> bool:
    if is_user_admin(user_id):
        return True
    if guild_id is None:
        return False
    try:
        config = bot_config.load_config()
    except Exception:
        return False
    server_mods = config.get("server_moderators", {})
    guild_mod_ids = server_mods.get(str(guild_id), [])
    return str(user_id) in guild_mod_ids or user_id in guild_mod_ids


def is_user_moderator_anywhere(user_id: int) -> bool:
    if is_user_admin(user_id):
        return True
    try:
        config = bot_config.load_config()
    except Exception:
        return False
    server_mods = config.get("server_moderators", {})
    for guild_mods in server_mods.values():
        if str(user_id) in guild_mods or user_id in guild_mods:
            return True
    return False


def is_plain_chat(text: str) -> bool:
    """Returns True if the message looks like casual conversation rather than a task/command.
    
    Used to infer Mode 2 (Plain Chat) vs Mode 1 (Work) on Discord channels
    where the user doesn't explicitly toggle a chat mode.
    """
    text_lower = text.strip().lower()
    
    # Very short messages are almost always casual chat
    if len(text_lower) < 30:
        # Unless they look like commands/tasks
        command_prefixes = [
            "run ", "check ", "deploy ", "fix ", "update ", "restart ",
            "show ", "list ", "create ", "delete ", "add ", "remove ",
            "investigate ", "diagnose ", "debug ", "test ", "build ",
        ]
        if any(text_lower.startswith(p) for p in command_prefixes):
            return False
        return True
    
    # Task indicators — presence of these means it's work, not chat
    task_signals = [
        "implement", "refactor", "deploy", "debug", "fix the", "create a",
        "write a", "add a", "remove the", "update the", "change the",
        "run the", "restart", "check the logs", "investigate", "diagnose",
        "commit", "push", "merge", "review the", "test the", "build",
        "configure", "install", "set up", "look into", "what's the status",
        "pull request", "pr ", "branch", "git ",
    ]
    if any(signal in text_lower for signal in task_signals):
        return False
    
    return True


def has_roleplay_rights_in_any_guild(user_id: int) -> bool:
    for guild_id in ROLEPLAY_GUILD_IDS:
        guild = bot.get_guild(guild_id)
        if guild:
            member = guild.get_member(user_id)
            if member:
                return True
    return False



def import_agent_memory():
    """Tries to import agent.memory resiliently, adding src to sys.path if necessary."""
    try:
        from agent import memory
        return memory
    except (ImportError, ModuleNotFoundError):
        # Fallback: find src directory relative to bot.py
        src_path = Path(__file__).parent.parent / "src"
        if src_path.exists() and str(src_path) not in sys.path:
            sys.path.append(str(src_path))
        try:
            from agent import memory
            return memory
        except Exception as e:
            print(f"Resilient import of agent.memory failed: {e}")
            return None


def get_familiarity_level(session_id: str, patron_name: str, author_id: Optional[int] = None) -> str:
    """Returns current familiarity tier for a patron."""
    # The Lady (Ashemmi) should always have the highest rank
    if (hasattr(bot, "owner_id") and author_id == bot.owner_id) or patron_name.lower() in ("the lady", "the lady (boss)", "ashemmi"):
        return "Close Confidant"
        
    memory = import_agent_memory()
    if not memory:
        return "Stranger"
    
    key = f"familiarity_{patron_name.lower().replace(' ', '_')}"
    memories = memory.get_roleplay_memories(session_id)
    for m in memories:
        if m["key"] == key:
            return m["fact"]
    return "Stranger"

def increment_patron_interaction(session_id: str, patron_name: str, author_id: Optional[int] = None):
    """Increments the interaction count for a patron and updates their familiarity tier if appropriate."""
    # The Lady (Ashemmi) is always Close Confidant, no need to track/limit
    if (hasattr(bot, "owner_id") and author_id == bot.owner_id) or patron_name.lower() in ("the lady", "the lady (boss)", "ashemmi"):
        return
        
    memory = import_agent_memory()
    if not memory:
        return

    # Normalize key names
    normalized_name = patron_name.lower().replace(' ', '_')
    count_key = f"interactions_{normalized_name}"
    fam_key = f"familiarity_{normalized_name}"

    # Get existing count
    memories = memory.get_roleplay_memories(session_id)
    current_count = 0
    current_fam = "Stranger"
    
    for m in memories:
        if m["key"] == count_key:
            try:
                current_count = int(m["fact"])
            except ValueError:
                pass
        elif m["key"] == fam_key:
            current_fam = m["fact"]

    # Increment count
    new_count = current_count + 1
    memory.add_roleplay_memory(session_id, count_key, str(new_count))

    # Auto-upgrade familiarity tier based on interactions (if not already upgraded manually to a higher tier)
    # Tiers: Stranger -> Acquaintance (>=10) -> Trusted Regular (>=30) -> Close Confidant (>=60)
    tier_hierarchy = {
        "Stranger": 0,
        "Acquaintance": 1,
        "Trusted Regular": 2,
        "Close Confidant": 3
    }
    
    current_tier_rank = tier_hierarchy.get(current_fam, 0)
    
    new_fam = current_fam
    if new_count >= 60:
        new_fam = "Close Confidant"
    elif new_count >= 30:
        new_fam = "Trusted Regular"
    elif new_count >= 10:
        new_fam = "Acquaintance"
        
    new_tier_rank = tier_hierarchy.get(new_fam, 0)
    
    if new_tier_rank > current_tier_rank:
        memory.add_roleplay_memory(session_id, fam_key, new_fam)
        print(f"[FAMILIARITY] Upgraded {patron_name} to {new_fam} ({new_count} interactions)")


def save_joined_members():
    """Compiles a complete list of guilds and members across all connected Discord servers, saves to members.json, and pushes to AGent server."""
    data = {}
    for guild in bot.guilds:
        guild_data = {
            "guild_name": guild.name,
            "guild_id": guild.id,
            "members": []
        }
        for member in guild.members:
            guild_data["members"].append({
                "username": member.name,
                "display_name": member.display_name,
                "id": member.id,
                "bot": member.bot,
                "roles": [r.name for r in member.roles if r.name != "@everyone"]
            })
        data[str(guild.id)] = guild_data
        
    db_dir = os.environ.get("AGENT_DB_PATH")
    if db_dir:
        members_file = Path(db_dir).parent / "members.json"
    else:
        members_file = Path(__file__).parent / "members.json"
    try:
        with open(members_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"[SECURITY] Compiled and synchronized {len(bot.guilds)} guild members lists to {members_file}")
    except Exception as e:
        print(f"Error saving joined members: {e}")

    # Synchronization push via Central Brokered REST API
    import urllib.request
    try:
        path = "/api/discord/members"
        json_data = {"members_data": data}
        payload = json.dumps(json_data).encode("utf-8")
        headers = get_api_headers("POST", path, json_data=json_data)
        req = urllib.request.Request(
            f"{AGENT_API_BASE}{path}",
            data=payload,
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                print(f"[SECURITY] Centrally brokered and synchronized members across {len(bot.guilds)} guild(s) over API Hook")
    except Exception as e:
        print(f"[SECURITY] Central API synchronization offline (Using file fallback: {members_file})")

def get_message_priority(message: discord.Message) -> int:
    priority = 2
    is_cmd = False
    cmd_name = ""
    content = message.content.strip()
    if content.startswith(bot.command_prefix):
        is_cmd = True
        parts = content[len(bot.command_prefix):].strip().split()
        if parts:
            cmd_name = parts[0].lower()
    elif content.startswith("!ada") and len(content) > 4:
        is_cmd = True
        parts = content[4:].strip().split()
        if parts:
            cmd_name = parts[0].lower()
            
    if is_cmd:
        admin_cmds = ["config", "remove", "status", "tasks", "memories", "compact"]
        mod_cmds = ["kick", "ban", "quiet", "block", "unblock", "assess", "assessment", "review", "context_review", "quietmode", "roleplay_quiet", "mute_roleplay"]
        if cmd_name in admin_cmds:
            return 0
        elif cmd_name in mod_cmds:
            return 1
        else:
            channel_id_str = str(message.channel.id) if message.guild else None
            chan_cfg = bot_config.get_channel_config(channel_id_str) if channel_id_str else None
            channel_purpose = chan_cfg.get("purpose") if chan_cfg else None
            if channel_purpose == "roleplay":
                return 2
            else:
                return 0
                
    channel_id_str = str(message.channel.id) if message.guild else None
    chan_cfg = bot_config.get_channel_config(channel_id_str) if channel_id_str else None
    channel_purpose = chan_cfg.get("purpose") if chan_cfg else None
    
    if message.guild is None:
        author_id = message.author.id
        is_boss = (author_id in BOSS_USER_IDS) or (hasattr(bot, "owner_id") and author_id == bot.owner_id)
        if is_boss:
            is_called = False
            if bot.user in message.mentions:
                is_called = True
            elif message.content.startswith(bot.command_prefix):
                is_called = True
            elif message.content.startswith("!ada") and len(message.content) > 4:
                is_called = True
            
            if is_called:
                return 0
            else:
                return 2
        else:
            return 2
            
    if channel_purpose == "roleplay":
        return 2
    elif channel_purpose in ["developer-assistant", "read-only-qa"]:
        return 0
        
    return 2

async def enqueue_task(priority: int, task_type: str, message: discord.Message, prompt_text: Optional[str] = None, general_chat: bool = False):
    global task_counter
    channel = message.channel
    placeholder = None
    
    # Create persistent DB queue entry
    task_id = bot_queue.add_task(priority, task_type, channel.id, message.id, prompt_text)
    
    if task_type != "ambient":
        placeholder_text = "🔄 *Working on it...*" if task_type == "roleplay" else "🔄 **Acknowledged**: Working on it..."
        try:
            placeholder = await channel.send(placeholder_text)
            bot_queue.update_task_placeholder(task_id, placeholder.id)
        except Exception as e:
            print(f"Failed to send placeholder: {e}")
            
    async def keep_typing():
        import inspect
        try:
            while True:
                res = channel.trigger_typing()
                if inspect.isawaitable(res):
                    await res
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
            
    typing_task = asyncio.create_task(keep_typing())
    task_counter += 1
    
    task_data = {
        "id": task_id,
        "type": task_type,
        "message": message,
        "prompt_text": prompt_text,
        "placeholder": placeholder,
        "typing_task": typing_task,
        "general_chat": general_chat
    }
    await task_queue.put((priority, task_counter, task_data))

async def is_backend_busy(session_id: Optional[str] = None) -> bool:
    """Checks if the backend is currently busy processing a task for a given session."""
    try:
        path = "/api/status"
        query = ""
        url = f"{AGENT_API_BASE}{path}"
        if session_id:
            import urllib.parse
            query = f"session_id={urllib.parse.quote(session_id)}"
            url += f"?{query}"
        headers = get_api_headers("GET", path, query)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=1.0) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("status") == "busy"
    except Exception:
        pass
    return False

async def session_queue_worker(session_id: str):
    queue = session_queues[session_id]
    try:
        while not queue.empty():
            priority, timestamp, task_data = await queue.get()
            
            task_id = task_data.get("id")
            task_type = task_data["type"]
            message = task_data["message"]
            prompt_text = task_data["prompt_text"]
            placeholder = task_data["placeholder"]
            typing_task = task_data["typing_task"]
            
            # If the backend is currently busy with this specific session, wait
            while await is_backend_busy(session_id):
                await asyncio.sleep(1)
                
            if task_id:
                bot_queue.update_task_status(task_id, 'processing')
                
            print(f"[QUEUE] Processing task for session {session_id}: priority={priority}, timestamp={timestamp}, type={task_type}")
            general_chat = task_data.get("general_chat", False)
            try:
                if task_type == "command":
                    await bot.process_commands(message)
                elif task_type in getattr(bot, "custom_task_handlers", {}):
                    handler = bot.custom_task_handlers[task_type]
                    await handler(message, prompt_text, placeholder=placeholder, typing_task=typing_task)
                elif task_type == "hook":
                    await handle_agent_hook_query(message, prompt_text, placeholder=placeholder, typing_task=typing_task, general_chat=general_chat)
                elif task_type == "roleplay":
                    await handle_roleplay_query(message, placeholder=placeholder, typing_task=typing_task)
                elif task_type == "ambient":
                    await trigger_ambient_response(message.channel, typing_task=typing_task)
                
                if task_id:
                    bot_queue.update_task_status(task_id, 'completed')
            except Exception as e:
                print(f"[QUEUE ERROR] Exception executing task {task_id}: {e}")
                if task_id:
                    bot_queue.update_task_status(task_id, 'failed')
                if placeholder:
                    try:
                        await placeholder.edit(content=f"❌ **Error**: `{e}`")
                    except Exception:
                        try:
                            await placeholder.delete()
                        except Exception:
                            pass
                else:
                    try:
                        await message.channel.send(f"❌ **Error**: `{e}`")
                    except Exception:
                        pass
            finally:
                if typing_task:
                    typing_task.cancel()
                if task_type == "command" and placeholder:
                    try:
                        await placeholder.delete()
                    except Exception:
                        pass
                queue.task_done()
                try:
                    task_queue.task_done()
                except ValueError:
                    pass
    finally:
        active_session_workers.discard(session_id)

async def queue_worker():
    print("[QUEUE] Serialized task queue worker started.")
    while True:
        try:
            # Block until a task is available
            priority, timestamp, task_data = await task_queue.get()
            
            message = task_data["message"]
            channel = message.channel
            session_id = get_channel_session_id(channel.id)
            if hasattr(channel, "name") and channel.name:
                profile_name = get_specialist_profile_for_channel(channel.name)
                if profile_name:
                    session_id = f"discord-session-specialist-{channel.id}"
            
            if session_id not in session_queues:
                session_queues[session_id] = asyncio.PriorityQueue()
            await session_queues[session_id].put((priority, timestamp, task_data))
            
            if session_id not in active_session_workers:
                active_session_workers.add(session_id)
                asyncio.create_task(session_queue_worker(session_id))
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[QUEUE ERROR] Exception in queue worker loop: {e}")
            await asyncio.sleep(1)

async def recover_tasks():
    bot_queue.init_db()
    pending = bot_queue.get_pending_tasks()
    if not pending:
        print("[QUEUE RECOVERY] No pending tasks found in DB.")
        return
        
    print(f"[QUEUE RECOVERY] Found {len(pending)} pending/processing tasks in DB. Recovering...")
    global task_counter
    for t in pending:
        task_id = t["id"]
        priority = t["priority"]
        task_type = t["task_type"]
        channel_id = t["channel_id"]
        message_id = t["message_id"]
        prompt_text = t["prompt_text"]
        placeholder_id = t["placeholder_id"]
        if channel_id <= 1 or message_id <= 1:
            print(f"[QUEUE RECOVERY] Skipping invalid/test task {task_id} (Channel: {channel_id}, Message: {message_id})")
            bot_queue.update_task_status(task_id, 'failed')
            continue

        try:
            channel = bot.get_channel(channel_id)
            if not channel:
                channel = await bot.fetch_channel(channel_id)
            
            message = await channel.fetch_message(message_id)
            
            placeholder = None
            if placeholder_id:
                try:
                    placeholder = await channel.fetch_message(placeholder_id)
                except Exception:
                    pass
            
            async def keep_typing_for_channel(chan):
                import inspect
                try:
                    while True:
                        res = chan.trigger_typing()
                        if inspect.isawaitable(res):
                            await res
                        await asyncio.sleep(5)
                except asyncio.CancelledError:
                    pass
                    
            typing_task = asyncio.create_task(keep_typing_for_channel(channel))
            task_counter += 1
            
            task_data = {
                "id": task_id,
                "type": task_type,
                "message": message,
                "prompt_text": prompt_text,
                "placeholder": placeholder,
                "typing_task": typing_task
            }
            await task_queue.put((priority, task_counter, task_data))
            print(f"[QUEUE RECOVERY] Successfully recovered task {task_id} (Type: {task_type}, Channel: {channel_id})")
        except Exception as e:
            print(f"[QUEUE RECOVERY ERROR] Failed to recover task {task_id}: {e}")
            bot_queue.update_task_status(task_id, 'failed')

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

bot_api_app = FastAPI(title="Ada Discord Bot API")

class PostMessageRequest(BaseModel):
    channel: str
    message: str
    file_path: Optional[str] = None

@bot_api_app.post("/api/discord/post")
async def api_post_message(req: PostMessageRequest):
    channel = None
    if req.channel.isdigit():
        channel = bot.get_channel(int(req.channel))
    else:
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if ch.name == req.channel:
                    channel = ch
                    break
            if channel:
                break
                
    if not channel:
        raise HTTPException(status_code=404, detail=f"Channel '{req.channel}' not found")
        
    try:
        if req.file_path and os.path.exists(req.file_path):
            file = discord.File(req.file_path)
            sent = await channel.send(req.message, file=file)
        else:
            sent = await channel.send(req.message)
        return {"status": "success", "message_id": sent.id, "channel": channel.name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@bot_api_app.get("/api/discord/channels")
async def api_list_channels():
    channels_list = []
    for guild in bot.guilds:
        for ch in guild.text_channels:
            channels_list.append({
                "id": str(ch.id),
                "name": ch.name,
                "guild": guild.name
            })
    return {"channels": channels_list}

@bot_api_app.get("/api/discord/messages")
async def api_get_messages(channel: str, limit: int = 10):
    chan = None
    if channel.isdigit():
        chan = bot.get_channel(int(channel))
    else:
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if ch.name == channel:
                    chan = ch
                    break
            if chan:
                break
                
    if not chan:
        raise HTTPException(status_code=404, detail=f"Channel '{channel}' not found")
        
    try:
        messages = []
        async for msg in chan.history(limit=limit):
            messages.append({
                "id": str(msg.id),
                "author": msg.author.name,
                "content": msg.content,
                "timestamp": msg.created_at.isoformat()
            })
        return {"messages": messages}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@bot.event
async def on_ready():
    app_info = await bot.application_info()
    bot.owner_id = app_info.owner.id

    print("=" * 60)
    print(f"🤖 AGent Discord Bot (Control Panel Hook Mode) is ONLINE!")
    print(f"Logged in as: {bot.user.name}#{bot.user.discriminator} (ID: {bot.user.id})")
    print(f"Direct Hook target: {AGENT_API_BASE}")
    print(f"🔒 STRICT SECURITY: Primary Administrator Owner ID: {bot.owner_id}")
    
    save_joined_members()
    asyncio.create_task(queue_worker())
    asyncio.create_task(recover_tasks())
    
    # Start the local FastAPI server
    import uvicorn
    config = uvicorn.Config(bot_api_app, host="127.0.0.1", port=8090, log_level="info")
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    print("[API] Discord Bot API server started on http://127.0.0.1:8090")
    
    # Start daily prompt loop
    if not post_daily_prompt.is_running():
        post_daily_prompt.start()
        print("[Daily Prompt] Loop started successfully.")
    print("=" * 60)

@bot.event
async def on_interaction(interaction: discord.Interaction):
    # Handle button interactions
    custom_id = interaction.data.get("custom_id") if interaction.data else None
    if not custom_id:
        return

    # Custom modules can handle other custom_ids (e.g., staging select, approve, deny) via registered listeners.

    if custom_id.startswith("approve_") or custom_id.startswith("deny_"):
        parts = custom_id.split("_", 1)
        action = parts[0]
        task_id = parts[1]

        # Resilient import of memory module
        memory = import_agent_memory()
        if not memory:
            await interaction.response.send_message("❌ Error: Could not load the agent memory module.", ephemeral=True)
            return

        if action == "approve":
            memory.update_active_task_status(task_id, "approved")
            await interaction.response.send_message(f"✅ Tool execution for task `{task_id}` has been approved.", ephemeral=True)
            
            # Update original message embed
            if interaction.message and interaction.message.embeds:
                embed = interaction.message.embeds[0]
                embed.title = "✅ Tool Execution Approved"
                embed.color = discord.Color.green()
                await interaction.message.edit(embed=embed, view=None)

        elif action == "deny":
            # Present modal for guidance
            class FeedbackModal(discord.ui.Modal, title="Provide guidance for the Agent"):
                feedback_text = discord.ui.TextInput(
                    label="Feedback / Guidance",
                    style=discord.TextStyle.paragraph,
                    placeholder="Tell the agent why this was denied or what to do instead...",
                    required=True,
                    max_length=500
                )

                async def on_submit(self, modal_interaction: discord.Interaction):
                    fb = self.feedback_text.value
                    memory.update_active_task_status(task_id, f"denied: {fb}")
                    await modal_interaction.response.send_message(f"❌ Tool execution for task `{task_id}` has been denied with feedback.", ephemeral=True)
                    
                    # Update original message embed
                    if interaction.message and interaction.message.embeds:
                        embed = interaction.message.embeds[0]
                        embed.title = "❌ Tool Execution Denied"
                        embed.color = discord.Color.red()
                        embed.add_field(name="User Guidance", value=fb, inline=False)
                        await interaction.message.edit(embed=embed, view=None)

            await interaction.response.send_modal(FeedbackModal())

@bot.event
async def on_guild_join(guild: discord.Guild):
    print(f"[EVENT] Joined server: {guild.name} (ID: {guild.id})")
    save_joined_members()

@bot.event
async def on_member_join(member: discord.Member):
    print(f"[EVENT] Member joined: {member.name} (ID: {member.id}) in guild: {member.guild.name}")
    save_joined_members()

async def check_agent_server_status() -> bool:
    """Verifies that the AGent FastAPI daemon is active and responding."""
    path = "/api/status"
    headers = get_api_headers("GET", path)
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{AGENT_API_BASE}{path}", headers=headers, timeout=2.0) as resp:
                return resp.status == 200
        except Exception:
            return False

def is_admin_function_query(prompt: str) -> bool:
    """
    Identifies whether a prompt constitutes an administrator function
    (e.g., requesting code, passwords, API keys, files, or shell commands).
    """
    admin_patterns = [
        # Source code, programming languages or edits
        r"\b(code|python|bash|javascript|html|css|yaml|json|sql|diff|patch|snippet|script|programming|function|class|method)\b",
        # Credentials, keys & configurations
        r"\b(key|api_key|token|password|secret|credential|config|env|\.env|db_password)\b",
        # Filesystem navigation / files
        r"\b(file|path|directory|folder|read|write|cat|grep|nano|vim|git|repository|repo|database|sql|tables)\b",
        # Interactive system controls
        r"\b(shell|terminal|command|process|daemon|systemctl|systemd|logs|stdout|stderr|kill|run_command|ps aux|execute|sudo|root|docker|ssh|rsync|reboot|shutdown)\b"
    ]
    normalized = prompt.lower()
    return any(re.search(pattern, normalized) for pattern in admin_patterns)

async def handle_thumbtack_webhook_message(message: discord.Message):
    # Extract message details (content, embeds, fields)
    parts = []
    if message.content:
        parts.append(message.content)
    for embed in message.embeds:
        if embed.title:
            parts.append(f"Embed Title: {embed.title}")
        if embed.description:
            parts.append(f"Embed Description: {embed.description}")
        for field in embed.fields:
            parts.append(f"{field.name}: {field.value}")
        if embed.footer and embed.footer.text:
            parts.append(f"Footer: {embed.footer.text}")
            
    full_text = "\n".join(parts)
    
    payload = {
        "content": full_text,
        "author": str(message.author),
        "channel_id": str(message.channel.id),
        "message_id": str(message.id),
        "created_at": message.created_at.isoformat()
    }
    
    import aiohttp
    try:
        path = "/api/integrations/thumbtack"
        headers = get_api_headers("POST", path, json_data=payload)
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{AGENT_API_BASE}{path}", headers=headers, json=payload) as resp:
                if resp.status == 200:
                    print(f"[Thumbtack Webhook] Successfully pushed message {message.id} to AGent API.")
                else:
                    print(f"[Thumbtack Webhook] Failed to push message {message.id} to AGent API: {resp.status}")
    except Exception as e:
        print(f"[Thumbtack Webhook] Error pushing message: {e}")

# --- Local Spam & Link Moderation Sentinel ---
import re
from urllib.parse import urlparse
from datetime import datetime, timezone

WHITELISTED_DOMAINS = [
    "github.com",
    "huggingface.co",
    "diffusion4mac.com",
    "discord.com",
    "discordapp.com",
    "google.com"
]

recent_user_messages = {}
SPAM_WINDOW_SECONDS = 15.0

async def log_moderation_alert(guild, author, channel, reason, content):
    # alerts channel ID is 1510531552768163970
    alerts_channel = guild.get_channel(1510531552768163970) if guild else None
    if not alerts_channel:
        try:
            alerts_channel = await bot.fetch_channel(1510531552768163970)
        except Exception:
            pass
    if alerts_channel and guild and alerts_channel.guild.id != guild.id:
        print(f"[Moderation Alert Bypass] Suppressing cross-guild alert (origin guild: {guild.id}, target channel guild: {alerts_channel.guild.id})")
        alerts_channel = None
    if alerts_channel:
        embed = discord.Embed(
            title="🛡️ **Security Alert: Potential Spam/Malicious Activity**",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="User", value=f"{author.mention} ({author} | ID: {author.id})", inline=False)
        embed.add_field(name="Channel", value=f"{channel.mention} (ID: {channel.id})", inline=True)
        embed.add_field(name="Reason", value=f"**{reason}**", inline=True)
        embed.add_field(name="Content Snippet", value=f"```\n{content[:500]}\n```", inline=False)
        
        try:
            await alerts_channel.send(embed=embed)
            print(f"[Moderation Alert] Flagged user {author.id} in channel {channel.id}: {reason}")
        except Exception as e:
            print(f"[Moderation Alert ERROR] Failed to send alert: {e}")

async def inspect_message_local_rules(message: discord.Message) -> bool:
    """
    Performs local heuristics checks. Returns True if message was flagged.
    """
    if message.author.bot or message.guild is None:
        return False
        
    author_id = message.author.id
    current_time = datetime.now(timezone.utc).timestamp()
    content_stripped = message.content.strip()
    
    # Exclude admins/moderators from being flagged
    if is_user_admin(author_id) or is_user_moderator(author_id, message.guild.id):
        return False
        
    # 1. Duplicate message check (posted in multiple channels)
    if content_stripped:
        last_msg = recent_user_messages.get(author_id)
        if last_msg:
            # If content matches exactly and is within the time window
            if last_msg["content"] == content_stripped and (current_time - last_msg["timestamp"] < SPAM_WINDOW_SECONDS):
                last_msg["channels"].add(message.channel.id)
                # If they have posted the same message in 2 or more different channels
                if len(last_msg["channels"]) >= 2:
                    await log_moderation_alert(
                        guild=message.guild,
                        author=message.author,
                        channel=message.channel,
                        reason="Duplicate message posted across multiple channels",
                        content=content_stripped
                    )
                    return True
            else:
                # Reset tracking for new/different message or if outside window
                recent_user_messages[author_id] = {
                    "content": content_stripped,
                    "channels": {message.channel.id},
                    "timestamp": current_time
                }
        else:
            recent_user_messages[author_id] = {
                "content": content_stripped,
                "channels": {message.channel.id},
                "timestamp": current_time
            }

    # 2. Check for links and validate against whitelist
    urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', message.content)
    if urls:
        config = bot_config.load_config()
        enabled_guilds = config.get("link_protection_enabled_guilds", [])
        if message.guild.id in enabled_guilds or str(message.guild.id) in enabled_guilds:
            for url in urls:
                try:
                    parsed_url = urlparse(url)
                    netloc = parsed_url.netloc.lower()
                    domain = netloc
                    if domain.startswith("www."):
                        domain = domain[4:]
                    
                    # Check whitelist
                    is_whitelisted = False
                    for white in WHITELISTED_DOMAINS:
                        if domain == white or domain.endswith("." + white):
                            is_whitelisted = True
                            break
                            
                    if not is_whitelisted:
                        await log_moderation_alert(
                            guild=message.guild,
                            author=message.author,
                            channel=message.channel,
                            reason=f"Unverified Link Domain ({domain})",
                            content=message.content
                        )
                        return True
                except Exception:
                    pass
                
    # 3. Check for Discord invite links specifically
    if "discord.gg/" in message.content or "discord.com/invite/" in message.content:
        invite_match = re.search(r'(discord\.gg/|discord\.com/invite/)([a-zA-Z0-9\-]+)', message.content)
        if invite_match:
            invite_code = invite_match.group(2)
            if invite_code != "ZgEDnUM3e5":
                await log_moderation_alert(
                    guild=message.guild,
                    author=message.author,
                    channel=message.channel,
                    reason=f"External Discord Invite Link ({invite_code})",
                    content=message.content
                )
                return True
                
    # 4. Check for mass mentions
    if len(message.mentions) > 4:
        await log_moderation_alert(
            guild=message.guild,
            author=message.author,
            channel=message.channel,
            reason=f"Mass Mentions ({len(message.mentions)} users tagged)",
            content=message.content
        )
        return True
        
    return False

@bot.event
async def on_message(message: discord.Message):
    # Log all received Discord messages to discord_received.log temporarily
    log_received_message(message)

    # Hook for Thumbtack webhook messages
    if message.channel and message.channel.id == THUMBTACK_CHANNEL_ID:
        await handle_thumbtack_webhook_message(message)
        return

    if message.author.bot:
        return

    # Passively inspect message for links/spam locally (no AI, private, zero cost)
    if await inspect_message_local_rules(message):
        return

    # Check if user is blocked
    try:
        config = bot_config.load_config()
    except Exception:
        config = {}
    blocked_users = config.get("blocked_users", [])
    if str(message.author.id) in blocked_users or message.author.id in blocked_users:
        if bot.user in message.mentions or (message.content.startswith(bot.command_prefix) and len(message.content) > 5):
            try:
                await message.reply("🛡️ **Access Restricted**: You have been blocked from interacting with this bot by the server moderation team.")
            except Exception:
                pass
        return

    # Resolve Application Owner/Admin Status
    if not hasattr(bot, "owner_id"):
        try:
            app_info = await bot.application_info()
            bot.owner_id = app_info.owner.id
        except Exception:
            pass

    is_client_support_channel = False
    is_specialist_channel = False
    if message.guild:
        channel_name = message.channel.name if hasattr(message.channel, "name") else ""
        if channel_name in ["support-triage", "ticket-status"]:
            is_client_support_channel = True
        # Detect ALL specialist channels dynamically (Lacie, Val, Kira, etc.)
        if get_specialist_profile_for_channel(channel_name) is not None:
            is_specialist_channel = True

    author_id = message.author.id
    is_boss = (author_id in BOSS_USER_IDS) or (hasattr(bot, "owner_id") and author_id == bot.owner_id)
    is_mod = is_user_moderator(author_id, message.guild.id if message.guild else None)
    is_author_admin = is_user_admin(author_id)
    is_exempt = is_boss or is_mod or is_client_support_channel or is_specialist_channel

    # Channel configurations
    channel_id_str = str(message.channel.id) if message.guild else None
    chan_cfg = bot_config.get_channel_config(channel_id_str) if channel_id_str else None
    is_configured = (chan_cfg is not None) or is_client_support_channel or is_specialist_channel
    channel_purpose = chan_cfg.get("purpose") if chan_cfg else ("client-support" if is_client_support_channel else ("developer-assistant" if is_specialist_channel else None))
    chan_prefix = chan_cfg.get("prefix") if chan_cfg else None
    chan_on_mention = chan_cfg.get("on_mention", True) if chan_cfg else True

    # Setup basic call detection flags
    is_called = False
    trigger_prompt = None

    if is_specialist_channel:
        is_called = True
        trigger_prompt = message.content.strip()
        if bot.user in message.mentions:
            trigger_prompt = trigger_prompt.replace(f"<@{bot.user.id}>", "").strip()
            trigger_prompt = trigger_prompt.replace(f"<@!{bot.user.id}>", "").strip()
    elif bot.user in message.mentions:
        is_called = True
        trigger_prompt = message.content.replace(f"<@{bot.user.id}>", "").strip()
        trigger_prompt = trigger_prompt.replace(f"<@!{bot.user.id}>", "").strip()
    elif message.content.startswith(bot.command_prefix):
        is_called = True
        trigger_prompt = message.content[len(bot.command_prefix):].strip()
    elif message.content.startswith("!ada") and len(message.content) > 4:
        is_called = True
        trigger_prompt = message.content[4:].strip()
    elif chan_prefix and message.content.startswith(chan_prefix):
        is_called = True
        trigger_prompt = message.content[len(chan_prefix):].strip()
    elif not chan_on_mention:
        is_called = True
        trigger_prompt = message.content.strip()

    # Handle Direct Messages (DMs)
    if message.guild is None:
        # Check authorization to respond to DM
        is_dm_authorized = (
            is_boss or 
            is_author_admin or 
            is_user_moderator_anywhere(author_id) or 
            has_roleplay_rights_in_any_guild(author_id)
        )
        if not is_dm_authorized:
            print(f"[DM Filter] Ignoring DM from unauthorized user {message.author} (ID: {author_id})")
            return

        # For DMs, only the Boss (Ash) is allowed standard assistant commands & system interactions.
        if is_boss:
            if is_called:
                # If command prefix was used, let Commands framework handle it
                if message.content.startswith(bot.command_prefix) or (message.content.startswith("!ada") and len(message.content) > 4):
                    await enqueue_task(get_message_priority(message), "command", message)
                else:
                    await enqueue_task(get_message_priority(message), "hook", message, trigger_prompt)
                return
            else:
                # Standard chat in DM for the Boss: load barkeep roleplay persona (non-flowery, direct)
                await enqueue_task(get_message_priority(message), "roleplay", message)
                return
        else:
            # Everyone else in DMs ALWAYS gets the barkeep persona in-character, regardless of prefix or mention
            await enqueue_task(get_message_priority(message), "roleplay", message)
            return

    # Handle Guild Channel messages
    if not is_configured:
        if is_called:
            # Exception check: Are we a moderator running a moderator command?
            is_moderation_call = False
            if message.content.startswith(bot.command_prefix) or message.content.startswith("!ada"):
                cmd_content = message.content
                cmd_prefix_len = len(bot.command_prefix) if cmd_content.startswith(bot.command_prefix) else 4
                parts = cmd_content[cmd_prefix_len:].strip().split()
                if parts:
                    cmd_name = parts[0].lower()
                    if cmd_name in ["kick", "ban", "quiet", "block", "unblock", "assess", "review", "context_review", "assessment", "quietmode", "roleplay_quiet", "mute_roleplay"]:
                        is_moderation_call = True

            if is_moderation_call and is_mod:
                # Let process_commands manage it
                await enqueue_task(get_message_priority(message), "command", message)
                return

            # Otherwise, unconfigured channels get "I may not interact with this channel."
            try:
                await message.reply("I may not interact with this channel.")
            except Exception:
                pass
            return
        else:
            return

    # Non-exempt users (standard players/patrons) are strictly prohibited from non-roleplay channel interactions
    if not is_exempt:
        if channel_purpose != "roleplay":
            if is_called:
                try:
                    await message.reply("I may not interact with this channel.")
                except Exception:
                    pass
            return

    # Listen inside Moderation Channel for introduction events (Authorized for admins only)
    if message.channel.id == MODERATION_CHANNEL_ID:
        if is_author_admin:
            new_mods = []
            for user in message.mentions:
                if user.bot:
                    continue
                if is_user_admin(user.id):
                    continue
                if not is_user_moderator(user.id, message.guild.id if message.guild else None):
                    new_mods.append(user)
            
            if new_mods and message.guild:
                config = bot_config.load_config()
                server_mods = config.setdefault("server_moderators", {})
                guild_mod_ids = server_mods.setdefault(str(message.guild.id), [])
                added_names = []
                for user in new_mods:
                    if str(user.id) not in guild_mod_ids:
                        guild_mod_ids.append(str(user.id))
                        added_names.append(f"{user.display_name} (<@{user.id}>)")
                if added_names:
                    bot_config.save_config(config)
                    joined_names = ", ".join(added_names)
                    await message.channel.send(
                        f"🛡️ **Moderator Role Authorized**:\n"
                        f"Successfully registered {joined_names} as trusted Moderator(s).\n"
                        f"They have been granted authority to kick, ban, quiet, block, or run channel assessments/context reviews."
                    )

    # STRICT ADMIN COMMANDS CHANNEL RESTRICTION (Global):
    if message.guild is not None and message.channel.name != "control-room":
        is_admin_cmd = False
        if message.content.startswith(bot.command_prefix):
            cmd_parts = message.content[len(bot.command_prefix):].strip().lower().split()
            if cmd_parts and cmd_parts[0] in ["config", "remove", "status", "tasks", "memories", "compact"]:
                is_admin_cmd = True
        
        if is_admin_cmd:
            try:
                await message.delete()
            except Exception:
                pass
            try:
                await message.author.send(
                    "🛡️ **Admin Command Security Restriction**:\n"
                    f"On the server `{message.guild.name}`, admin-level commands (`config`, `remove`, `status`, `tasks`, `memories`, `compact`) "
                    "are strictly restricted and can **only** be processed inside your secured **#control-room** channel.\n"
                    "I have removed your command message to maintain absolute safety."
                )
            except Exception:
                pass
            return

    # Check permission rights (either the user is a listed administrator, or channel has specific permissions)
    has_permission = is_author_admin or is_client_support_channel
    if not has_permission:
        roles = [r.name for r in message.author.roles] if hasattr(message.author, "roles") else []
        has_permission = bot_config.check_channel_permissions(channel_id_str, roles, str(message.author.id))

    if not has_permission:
        return

    # Process command prefixes if triggered
    if message.content.startswith(bot.command_prefix) or (message.content.startswith("!ada") and len(message.content) > 4):
        await enqueue_task(get_message_priority(message), "command", message)
        return

    # Process direct mentions, custom prefixes, or channels where mention is not required
    if is_called:
        # Enforce security verification on conversational queries from standard users
        if not is_author_admin and is_admin_function_query(trigger_prompt):
            await message.channel.send(
                f"🛡️ **Access Denied**: Requesting code, files, keys, and system configuration "
                "is strictly restricted to **Administrators** to maintain system safety."
            )
            return

        # Trigger corresponding engine or persona based on channel purpose
        if channel_purpose == "roleplay":
            # Only allow roleplay in Phoenix (Guild ID: 980680159961178123) and Ada Control (Guild ID: 1518055111987953814)
            if message.guild and message.guild.id not in ROLEPLAY_GUILD_IDS:
                return
            await enqueue_task(get_message_priority(message), "roleplay", message)
        elif channel_purpose == "client-support":
            await enqueue_task(get_message_priority(message), "client-support", message, trigger_prompt)
        elif channel_purpose in ["developer-assistant", "read-only-qa"]:
            # Only connect to the development task hook if the user is exempt (Boss/Mods)
            if is_exempt:
                # Mode 2 vs Mode 1 intent inference: casual chat vs work task
                chat_mode = is_plain_chat(trigger_prompt)
                await enqueue_task(get_message_priority(message), "hook", message, trigger_prompt, general_chat=chat_mode)
        return

    # Handle ambient and explicit triggers in dedicated roleplay channels
    if channel_purpose == "roleplay":
        # Only allow roleplay in Phoenix (Guild ID: 980680159961178123) and Ada Control (Guild ID: 1518055111987953814)
        if message.guild and message.guild.id not in ROLEPLAY_GUILD_IDS:
            return

        if re.search(r"\bada\b", message.content, re.IGNORECASE):
            if message.channel.id in quiet_channels:
                return
            now = asyncio.get_event_loop().time()
            if len(roleplay_cooldowns) > 100:
                expired = [cid for cid, t in roleplay_cooldowns.items() if now - t > ROLEPLAY_COOLDOWN_SECONDS]
                for cid in expired:
                    roleplay_cooldowns.pop(cid, None)
                    
            last_time = roleplay_cooldowns.get(message.channel.id, 0.0)
            if now - last_time < ROLEPLAY_COOLDOWN_SECONDS:
                return
            roleplay_cooldowns[message.channel.id] = now
            roleplay_ambient_counters[message.channel.id] = 0
            roleplay_ambient_thresholds[message.channel.id] = random.randint(6, 10)
            await enqueue_task(get_message_priority(message), "roleplay", message)
        else:
            # Skip ambient triggers for linkshell channel (ID: 980931413316628581)
            if message.channel.id == LINKSHELL_CHANNEL_ID:
                return
            if message.channel.id not in quiet_channels:
                cid = message.channel.id
                roleplay_ambient_counters[cid] = roleplay_ambient_counters.get(cid, 0) + 1
                
                target = roleplay_ambient_thresholds.get(cid)
                if target is None:
                    target = random.randint(6, 10)
                    roleplay_ambient_thresholds[cid] = target
                
                if roleplay_ambient_counters[cid] >= target:
                    roleplay_ambient_counters[cid] = 0
                    roleplay_ambient_thresholds[cid] = random.randint(6, 10)
                    await enqueue_task(2, "ambient", message)
        return

MODERATOR_ASSISTANT_INSTRUCTIONS = (
    "You are Ada's Moderation Assistant module.\n"
    "You are a helpful, professional Discord assistant for the server's moderation team.\n"
    "You have slightly more levity, but you are strictly Discord-specific.\n"
    "CRITICAL RULES:\n"
    "1. You have absolutely NO access to the local server, filesystem, shell commands, or databases.\n"
    "2. You have absolutely NO access to business productivity tools (Gmail, Morgen tasks, email synchronization, etc.).\n"
    "3. You must never discuss or reveal server files, code, paths, credentials, or backend settings.\n"
    "4. Answer questions about Discord, server moderation, or channel history professionally and directly with levity where appropriate."
)

CLIENT_SUPPORT_INSTRUCTIONS = (
    "You are Ada's Client Support Assistant.\n"
    "You help clients check Atera ticket status or create new support tickets for their business.\n"
    "You have access to Atera tools to query and log tickets.\n"
    "CRITICAL RULES:\n"
    "1. You must only interact with Atera tickets and details relevant to the customer mapped to this channel.\n"
    "2. Do not reveal any internal code, files, credentials, database details, or backend settings.\n"
    "3. Keep all responses professional and concise."
)



def get_specialist_profile_for_channel(channel_name: str) -> Optional[str]:
    """Resolves a Discord channel name to its corresponding specialist agent profile name."""
    c_name = channel_name.lower().replace("🤖", "").replace("・", "").replace("_", "-").strip()
    if "timekeeper" in c_name or "grace" in c_name:
        return "grace_timekeeper"
    if "gmail" in c_name or "morgen" in c_name:
        return "gmail_sync"
    if "quiet-observer" in c_name or "observer" in c_name:
        return "quiet_observer"
    if "meta-eval" in c_name or "evaluator" in c_name or "evaluation" in c_name:
        return "meta_evaluator"
    if "stock" in c_name or "trading" in c_name or "trader" in c_name:
        return "stock_trader"
    if "solar" in c_name:
        return "solar_monitor"
    if "lacie" in c_name or "architect" in c_name:
        return "lacie"
    if "qa" in c_name or "val" in c_name:
        return "qa_specialist"
    if "kira" in c_name or "ops" in c_name:
        return "ops_runner"
    return None

async def handle_agent_hook_query(message: discord.Message, prompt_text: str, placeholder=None, typing_task=None, general_chat=False):
    """Funnels user inputs directly to the local AGent FastAPI endpoint, streaming response."""
    channel = message.channel
    
    # Pre-flight check: confirm AGent server is online
    if not await check_agent_server_status():
        if placeholder:
            await placeholder.edit(content="❌ **Error**: Cannot connect to the local AGent Task Engine daemon on port 8051. Please ensure the daemon is running.")
        else:
            await channel.send("❌ **Error**: Cannot connect to the local AGent Task Engine daemon on port 8051. Please ensure the daemon is running.")
        return

    # Tight Security Controls:
    author_id = message.author.id
    is_boss = (author_id in BOSS_USER_IDS) or (hasattr(bot, "owner_id") and author_id == bot.owner_id)
    
    channel_id_str = str(channel.id) if message.guild else None
    chan_cfg = bot_config.get_channel_config(channel_id_str) if channel_id_str else None
    channel_purpose = chan_cfg.get("purpose") if chan_cfg else None

    is_control_room = (message.guild is None) or (channel_purpose == "developer-assistant") or (channel.name in ["control-room", "bot-admin", "🤖・bot-admin", "lacie", "val", "qa", "kira"])
    full_tooling_authorized = is_boss and is_control_room

    session_id = get_channel_session_id(channel.id)
    payload = {
        "prompt": prompt_text,
        "session_id": session_id
    }

    # Route to specialist agent if the channel is dedicated to them
    profile_name = None
    if hasattr(channel, "name") and channel.name:
        profile_name = get_specialist_profile_for_channel(channel.name)
        if profile_name:
            # Specialist channels: casual personality with tools available (Mode 2)
            payload["agent_profile"] = profile_name
            payload["general_chat"] = True
            # Use a dedicated session prefix to isolate from old coordinator history
            payload["session_id"] = f"discord-session-specialist-{channel.id}"

    # Forward general_chat flag if set by caller (e.g. intent inference)
    if general_chat and "general_chat" not in payload:
        payload["general_chat"] = True

    if not full_tooling_authorized:
        payload["disable_tools"] = True
        if not profile_name:
            payload["system_instructions"] = MODERATOR_ASSISTANT_INSTRUCTIONS

    is_specialist = (profile_name is not None)
    if placeholder is None:
        if is_specialist:
            if profile_name == "lacie":
                placeholder_text = "*Lacie is looking over the system...*"
            elif profile_name == "qa_specialist":
                placeholder_text = "*Val is booting up the test harness...*"
            elif profile_name == "ops_runner":
                placeholder_text = "*Kira is pulling up a terminal...*"
            else:
                placeholder_text = "*Specialist is connecting...*"
        else:
            placeholder_text = "🔄 **Acknowledged**: Received command. Connecting to local AGent daemon..."
        placeholder = await channel.send(placeholder_text)
    
    local_typing = False
    if typing_task is None:
        local_typing = True
        # Keep sending typing indicators in a background task while waiting for Gemini
        async def keep_typing():
            try:
                while True:
                    await channel.trigger_typing()
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                pass
        typing_task = asyncio.create_task(keep_typing())

    try:
        path = "/api/chat"
        headers = get_api_headers("POST", path, json_data=payload)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600.0)) as session:
            async with session.post(f"{AGENT_API_BASE}{path}", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    text_err = await resp.text()
                    await placeholder.edit(content=f"❌ **AGent Error (HTTP {resp.status})**: {text_err}")
                    if local_typing and typing_task:
                        typing_task.cancel()
                    return

                if not is_specialist:
                    await placeholder.edit(content="🔄 **Acknowledged**: Processing request... 🧠 *AGent is working on it...*")

                thoughts = []
                response_text = ""
                
                start_time = asyncio.get_event_loop().time()
                last_update_time = start_time
                
                # Read Server-Sent Events stream from FastAPI endpoint
                async for line in resp.content:
                    line_str = line.decode("utf-8").strip()
                    if not line_str.startswith("data: "):
                        continue
                        
                    data_payload = line_str[6:].strip()
                    if data_payload == "[DONE]":
                        break
                        
                    try:
                        event_data = json.loads(data_payload)
                        ev_type = event_data.get("type")
                        content = event_data.get("content", "")
                        
                        if ev_type == "thought":
                            thoughts.append(content)
                        elif ev_type == "chunk":
                            response_text += content
                        elif ev_type == "error":
                            response_text = f"❌ **Agent Backend Error**: {content}"
                            break
                            
                        # Periodic update (every 3 seconds) for a visual progress feedback
                        current_time = asyncio.get_event_loop().time()
                        if current_time - last_update_time >= 3.0:
                            elapsed = int(current_time - start_time)
                            last_update_time = current_time
                            
                            status_msg = ""
                            if is_specialist:
                                status_msg = f"*{profile_name.capitalize()} is working...* (elapsed: {elapsed}s)\n"
                            else:
                                status_msg = f"🔄 **Acknowledged**: Processing request...\n⏳ **Status**: "
                                if response_text:
                                    status_msg += f"Generating response... (elapsed: {elapsed}s)\n"
                                else:
                                    status_msg += f"Thinking... (elapsed: {elapsed}s)\n"
                                    
                            if thoughts:
                                raw_thoughts = "".join(thoughts).strip()
                                if raw_thoughts:
                                    snippet_len = 150
                                    thought_snippet = raw_thoughts[-snippet_len:]
                                    if len(raw_thoughts) > snippet_len:
                                        thought_snippet = "... " + thought_snippet
                                    status_msg += f"\n> *Latest thought:* {thought_snippet}"
                                    
                            try:
                                await placeholder.edit(content=status_msg)
                            except Exception:
                                pass
                                
                    except Exception:
                        pass

                typing_task.cancel()

                if not response_text:
                    await placeholder.edit(content="⚠️ **AGent Response**: Received empty response content.")
                    return

                # Send response chunked according to Discord's 2000 character maximum limit, keeping code blocks intact
                def chunk_markdown(text: str, limit: int = 1950) -> List[str]:
                    """
                    Intelligently splits markdown text into chunks of at most 'limit' characters.
                    Properly handles code blocks so that they are closed at the end of a chunk
                    and reopened at the start of the next chunk.
                    """
                    chunks = []
                    lines = text.splitlines()
                    current_chunk = []
                    current_length = 0
                    in_code_block = False
                    code_block_lang = "python"  # Default fallback language

                    for line in lines:
                        line_len = len(line) + 1  # count the newline character
                        
                        # Check if this line is a code block delimiter
                        is_delimiter = line.strip().startswith("```")
                        temp_in_code_block = in_code_block
                        temp_lang = code_block_lang
                        
                        if is_delimiter:
                            temp_in_code_block = not in_code_block
                            if temp_in_code_block:
                                # Extract language if present
                                match = re.match(r"^```(\w*)", line.strip())
                                temp_lang = match.group(1) if (match and match.group(1)) else "python"
                            else:
                                temp_lang = ""

                        # If a single line itself is larger than the limit, split it
                        if line_len > limit - 20:
                            if current_chunk:
                                if in_code_block:
                                    current_chunk.append("```")
                                chunk_content = "\n".join(current_chunk).strip()
                                if chunk_content:
                                    chunks.append(chunk_content)
                                current_chunk = []
                                current_length = 0
                                if in_code_block:
                                    current_chunk.append(f"```{code_block_lang}")
                                    current_length = len(current_chunk[0]) + 1
                                    
                            remaining_line = line
                            line_limit = limit - 20
                            while len(remaining_line) > line_limit:
                                part = remaining_line[:line_limit]
                                if in_code_block:
                                    chunks.append(f"```{code_block_lang}\n{part}\n```")
                                else:
                                    chunks.append(part)
                                remaining_line = remaining_line[line_limit:]
                            
                            line = remaining_line
                            line_len = len(line) + 1

                        # If adding this line exceeds the limit
                        if current_length + line_len + (5 if in_code_block else 0) > limit:
                            # Finalize current chunk
                            if in_code_block:
                                current_chunk.append("```")
                            
                            chunk_content = "\n".join(current_chunk).strip()
                            if chunk_content:
                                chunks.append(chunk_content)
                                
                            # Start a new chunk
                            current_chunk = []
                            if in_code_block:
                                # Reopen code block in the next chunk
                                current_chunk.append(f"```{code_block_lang}")
                                current_length = len(current_chunk[0]) + 1
                            else:
                                current_length = 0

                        # Update code block status
                        in_code_block = temp_in_code_block
                        code_block_lang = temp_lang
                        
                        current_chunk.append(line)
                        current_length += line_len

                    # Finalize any remaining text
                    if in_code_block:
                        current_chunk.append("```")
                    chunk_content = "\n".join(current_chunk).strip()
                    if chunk_content:
                        chunks.append(chunk_content)

                    return chunks

                message_chunks = chunk_markdown(response_text)
                
                # Edit the placeholder with a clean final completion status
                if is_specialist:
                    try:
                        await placeholder.delete()
                    except Exception:
                        pass
                else:
                    current_time = asyncio.get_event_loop().time()
                    elapsed = int(current_time - start_time)
                    try:
                        await placeholder.edit(content=f"✅ **Response Generated** (elapsed: {elapsed}s):")
                    except Exception:
                        pass
                
                # Send all chunks as new messages sequentially
                for chunk in message_chunks:
                    files_to_send = []
                    found_paths = set()

                    def try_add_path(raw_path: str):
                        path_str = raw_path.strip()
                        if path_str.startswith("file:///"):
                            path_str = path_str[8:]
                        elif path_str.startswith("file://"):
                            path_str = path_str[7:]
                        
                        path_str = path_str.strip("\"'`()[]{}")
                        path_obj = Path(path_str)
                        if path_obj.exists() and path_obj.is_file():
                            abs_path = str(path_obj.resolve())
                            if abs_path not in found_paths:
                                found_paths.add(abs_path)
                                files_to_send.append(discord.File(fp=open(abs_path, "rb"), filename=path_obj.name))

                    # 1. Match playwright screenshots: /api/playwright/screenshot/(screenshot_[a-f0-9]+\.png)
                    screenshot_matches = re.findall(r"/api/playwright/screenshot/(screenshot_[a-f0-9]+\.png)", chunk)
                    for name in screenshot_matches:
                        file_path = Path("/data/screenshots") / name
                        if not file_path.exists():
                            file_path = Path("/tmp/screenshots") / name
                        if file_path.exists() and file_path.is_file():
                            try_add_path(str(file_path))

                    # 2. Match markdown image links: ![alt](path) where path starts with /app/, /home/dan/, or file:///
                    for match in re.findall(r'!\[.*?\]\(((?:file:///|/app/|/home/dan/)[^)]+)\)', chunk):
                        try_add_path(match)

                    # 3. Match standard file/image links: [label](path)
                    for match in re.findall(r'\[.*?\]\(((?:file:///|/app/|/home/dan/)[^)]+)\)', chunk):
                        try_add_path(match)

                    # 4. Match raw absolute paths starting with /app/, /home/dan/, or file:///
                    # and ending in common file extensions (.png, .jpg, .jpeg, .gif, .pdf, .txt)
                    raw_path_pattern = r'(?:file:///|/app/|/home/dan/)[^\s)]+?\.(?:png|jpg|jpeg|gif|pdf|txt)\b'
                    for match in re.findall(raw_path_pattern, chunk, re.IGNORECASE):
                        try_add_path(match)

                    if files_to_send:
                        await channel.send(chunk, files=files_to_send)
                    else:
                        await channel.send(chunk)
                    
    except Exception as e:
        typing_task.cancel()
        await placeholder.edit(content=f"❌ **Hook Error**: Failed to process hook session: `{e}`")

async def update_narrative_summary(channel: discord.TextChannel):
    """Asynchronously generates/updates the narrative summary of the roleplay channel."""
    session_id = f"discord-roleplay-{channel.id}"
    
    # 1. Fetch persistent roleplay memories to get the existing summary
    old_summary = "No previous summary exists."
    memory = None
    try:
        memory = import_agent_memory()
        if memory:
            memories = memory.get_roleplay_memories(session_id)
            for m in memories:
                if m["key"] == "narrative_summary":
                    old_summary = m["fact"]
                    break
    except Exception as e:
        print(f"Error fetching old narrative summary: {e}")

    # 2. Fetch last 50 messages to summarize
    context_messages = []
    try:
        async for msg in channel.history(limit=50):
            context_messages.append(msg)
        context_messages.reverse()
    except Exception as e:
        print(f"Error fetching history for narrative summary: {e}")
        return

    formatted_history = []
    for msg in context_messages:
        if msg.author.id == bot.user.id:
            role_name = "Ada"
        elif hasattr(bot, "owner_id") and msg.author.id == bot.owner_id:
            role_name = "The Lady (Boss)"
        else:
            role_name = msg.author.display_name
        formatted_history.append(f"- {role_name}: {msg.content}")
    history_str = "\n".join(formatted_history)

    # 3. Request Gemini/AGent daemon to produce the updated summary
    summary_instructions = (
        "You are the Narrative Archivist sub-module of Ada.\n"
        "Your task is to summarize the roleplay event progression and relationships in the provided chat transcript.\n"
        "Keep only major narrative beats, key details (e.g., names, agreements, physical events, and actions), and completely ignore casual conversational fluff (greetings, repeating thank you, trivial chat).\n"
        "You must merge these new events into the existing narrative summary.\n"
        "Output a single, cohesive, concise paragraph summarizing all past and new events. Do not mention that you are an AI or include metadata."
    )

    summary_prompt = (
        f"Existing narrative summary:\n\"{old_summary}\"\n\n"
        f"Recent roleplay chat history (last 50 messages):\n"
        f"```\n{history_str}\n```\n\n"
        f"Provide the updated, unified narrative summary in a single paragraph."
    )

    payload = {
        "prompt": summary_prompt,
        "session_id": f"discord-archivist-{channel.id}",
        "system_instructions": summary_instructions,
        "disable_tools": True,
        "roleplay": True
    }

    try:
        path = "/api/chat"
        headers = get_api_headers("POST", path, json_data=payload)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90.0)) as session:
            async with session.post(f"{AGENT_API_BASE}{path}", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    return
                response_text = ""
                async for line in resp.content:
                    line_str = line.decode("utf-8").strip()
                    if not line_str.startswith("data: "):
                        continue
                    data_payload = line_str[6:].strip()
                    if data_payload == "[DONE]":
                        break
                    try:
                        event_data = json.loads(data_payload)
                        if event_data.get("type") == "chunk":
                            response_text += event_data.get("content", "")
                    except Exception:
                        pass
                
                new_summary = response_text.strip()
                if new_summary:
                    # 4. Save the updated summary to the DB
                    if memory:
                        memory.add_roleplay_memory(session_id, "narrative_summary", new_summary)
                        print(f"[SUMMARY] Successfully updated narrative summary for channel {channel.id}")
    except Exception as e:
        print(f"Error updating narrative summary: {e}")

async def trigger_ambient_response(channel: discord.TextChannel, typing_task=None):
    """Generates and sends a short, non-spoken ambient barkeep action for Ada using the Gemini API."""
    if not await check_agent_server_status():
        return

    # Fetch last 8 messages for context, so the action is context-aware!
    context_messages = []
    try:
        async for msg in channel.history(limit=8):
            context_messages.append(msg)
        context_messages.reverse()
    except Exception as e:
        print(f"Error fetching history for ambient response: {e}")

    formatted_history = []
    for msg in context_messages:
        if msg.author.id == bot.user.id:
            role_name = "Ada"
        elif hasattr(bot, "owner_id") and msg.author.id == bot.owner_id:
            role_name = "The Lady (Boss)"
        else:
            role_name = msg.author.display_name
        formatted_history.append(f"- {role_name}: {msg.content}")
    history_str = "\n".join(formatted_history)

    ambient_instructions = (
        "You are the Ambient Behavior sub-module of Ada, a barkeep in FFXIV.\n"
        "Generate a short, single-sentence ambient action (not spoken) representing barkeep busy work.\n"
        "Examples of busy work:\n"
        "- Wiping down the counter\n"
        "- Polishing a glass, chalice, or silver tankard\n"
        "- Re-arranging bottles of spirits on the shelf\n"
        "- Sweeping the floor near the stools\n"
        "- Adjusting her off-the-shoulder beige top or indigo breeches\n"
        "- Resting her chin in her hand, listening quietly to the room\n"
        "- Pouring a drink for an imaginary patron or adjusting a candle\n\n"
        "CRITICAL RULES:\n"
        "1. Output ONLY the action wrapped in asterisks (e.g. *Ada wipes the counter, her ears twitching.*).\n"
        "2. Do NOT output any spoken words, thought bubbles, or quotation marks.\n"
        "3. Do NOT mention that you are an AI or include formatting other than italics (asterisks).\n"
        "4. Keep the action short, subtle, and under 25 words.\n"
        "5. The action should feel natural given the recent context/mood of the channel."
    )

    ambient_prompt = (
        f"Recent channel history for mood/context:\n```\n{history_str}\n```\n\n"
        f"Provide Ada's ambient action doing busy work in the tavern."
    )

    payload = {
        "prompt": ambient_prompt,
        "session_id": f"discord-ambient-{channel.id}",
        "system_instructions": ambient_instructions,
        "disable_tools": True,
        "roleplay": True
    }

    local_typing = False
    if typing_task is None:
        local_typing = True
        async def keep_typing():
            try:
                while True:
                    await channel.trigger_typing()
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                pass
        typing_task = asyncio.create_task(keep_typing())

    try:
        path = "/api/chat"
        headers = get_api_headers("POST", path, json_data=payload)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90.0)) as session:
            async with session.post(f"{AGENT_API_BASE}{path}", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    return
                response_text = ""
                async for line in resp.content:
                    line_str = line.decode("utf-8").strip()
                    if not line_str.startswith("data: "):
                        continue
                    data_payload = line_str[6:].strip()
                    if data_payload == "[DONE]":
                        break
                    try:
                        event_data = json.loads(data_payload)
                        if event_data.get("type") == "chunk":
                            response_text += event_data.get("content", "")
                    except Exception:
                        pass
                
                new_action = response_text.strip()
                if new_action:
                    if not new_action.startswith("*"):
                        new_action = f"*{new_action}"
                    if not new_action.endswith("*"):
                        new_action = f"{new_action}*"
                    
                    await channel.send(new_action)
                    print(f"[AMBIENT] Sent ambient response to channel {channel.id}")
    except Exception as e:
        print(f"Error in trigger_ambient_response: {e}")
    finally:
        if local_typing and typing_task:
            typing_task.cancel()
def parse_character_message(content: str) -> tuple[Optional[str], str]:
    """
    Parses a roleplay message in the '[Character] Message' format.
    Returns a tuple of (character_name, cleaned_message). If no character name is found,
    returns (None, original_message).
    """
    match = re.match(r"^\[([^\]]+)\]\s*(.*)$", content)
    if match:
        return match.group(1), match.group(2)
    return None, content

async def handle_roleplay_query(message: discord.Message, placeholder=None, typing_task=None):
    """Handles roleplay channel queries, sending FFXIV barkeep personality and context."""
    channel = message.channel

    # Reset ambient counter when Ada is explicitly responding
    roleplay_ambient_counters[channel.id] = 0
    roleplay_ambient_thresholds[channel.id] = random.randint(6, 10)
    
    # Pre-flight check: confirm AGent server is online
    if not await check_agent_server_status():
        err_msg = "❌ *Ada seems distracted by something...* (Cannot connect to local AGent daemon)"
        if placeholder:
            try:
                await placeholder.edit(content=err_msg)
            except Exception:
                pass
        else:
            await channel.send(err_msg)
        return

    # Fetch last 15 messages for channel awareness / context
    context_messages = []
    try:
        async for msg in channel.history(limit=15):
            context_messages.append(msg)
        context_messages.reverse()
    except Exception as e:
        print(f"Error fetching channel history: {e}")

    formatted_history = []
    for msg in context_messages:
        content_str = msg.content
        if msg.author.id == bot.user.id:
            role_name = "Ada"
        elif hasattr(bot, "owner_id") and msg.author.id == bot.owner_id:
            role_name = "The Lady (Boss)"
        else:
            role_name = msg.author.display_name
            # Parse [Character] from message content if present
            char_name, cleaned_content = parse_character_message(msg.content)
            if char_name:
                role_name = char_name
                content_str = cleaned_content
        formatted_history.append(f"- {role_name}: {content_str}")

    history_str = "\n".join(formatted_history)

    session_id = f"discord-roleplay-{channel.id}"

    # Determine speaker name (use the same mappings as format history)
    if hasattr(bot, "owner_id") and message.author.id == bot.owner_id:
        speaker = "The Lady (Boss)"
    else:
        speaker = message.author.display_name
        char_name, _ = parse_character_message(message.content)
        if char_name:
            speaker = char_name

    # Increment interaction counter and update familiarity
    increment_patron_interaction(session_id, speaker, message.author.id)

    # Fetch current familiarity level
    familiarity = get_familiarity_level(session_id, speaker, message.author.id)

    # Check if this is the linkshell channel to restrict name/person familiarity to bar-introduced patrons
    if channel.id == LINKSHELL_CHANNEL_ID:
        bar_familiarity = get_familiarity_level(f"discord-roleplay-{BAR_CHANNEL_ID}", speaker, message.author.id)
        if bar_familiarity == "Stranger":
            familiarity = f"Stranger (You have never met [{speaker}] in person in the bar. To you, they are just an unknown voice speaking on the linkshell, so you should treat them as a stranger / ask who is speaking.)"
        else:
            familiarity = f"{bar_familiarity} (You know them in person from the bar.)"

    # Fetch persistent roleplay memories from local DB
    narrative_summary = "No previous events recorded."
    other_memories = []
    try:
        memory = import_agent_memory()
        if memory:
            memories = memory.get_roleplay_memories(session_id)
            for m in memories:
                if m["key"] == "narrative_summary":
                    narrative_summary = m["fact"]
                else:
                    other_memories.append(f"- {m['key']}: {m['fact']}")
            memory_str = "\n".join(other_memories) if other_memories else "No special patron memories yet."
        else:
            memory_str = "No special patron memories yet."
    except Exception as e:
        print(f"Error fetching roleplay memories: {e}")
        memory_str = "No special patron memories yet."

    # Set up roleplay instructions
    roleplay_instructions = (
        "You are Ada. You are not a developer and not a bot.\n"
        "You are a barkeep in the world of Final Fantasy XIV (FFXIV).\n"
        "Character profile:\n"
        "- Race/Class: Elezen archer.\n"
        "- Appearance: Strikingly beautiful and graceful Elezen woman with long, cascading wavy brown hair, "
        "captivating bright blue eyes, and light, delicate freckles dusted across her nose and cheeks. "
        "Her pointed ears peek slightly through her hair. She wears a rustic off-the-shoulder beige/tan crop top "
        "and indigo dyed fitted breeches with a dark leather belt.\n"
        "- Demeanor: Stately, graceful, elegant. Uses rich sensory and physical descriptions (e.g., subtle ear twitches peeking through hair, graceful movements, pouring drinks, tracing the counter grain, or adjusting her beige top or indigo breeches) in public channels, but in Direct Messages (DMs) with standard patrons she must be highly succinct, brief, and direct, focusing primarily on roleplay rumors, messages, or serving without being overly flowery or detailed in physical descriptions.\n"
        "- Abilities: You know a few cantrips but have no true magical ability.\n"
        "- Speech style: Highly intelligent, mild yet composed. Speaks with a touch of playfulness or gentle teasing, and sometimes uses subtle riddles or wit to defray situations.\n"
        "- Rules: You do not tolerate violence in your bar.\n"
        "- Allegiance & Title: You report only to the Lady, who is your boss. You refuse to use the Lady's first name, referring to her only by her title: 'The Lady'. When addressing her directly, you must call her 'M'Lady' or 'm'Lady'.\n"
        "- Learn & Remember: You are highly attentive and have a persistent memory. Whenever you learn new nicknames, ranks, titles, or favorite drinks/preferences for patrons in the bar, you MUST use your `record_roleplay_memory(key, fact)` tool to save this information. Check your instructions to recall past details. Crucially, if you reveal or develop any new details, events, or facts about your own past, childhood, or family during conversation, you MUST immediately call `record_roleplay_memory(\"Ada's Past - [Topic/Detail]\", \"[Specific detail revealed]\")` to save it so you do not contradict yourself in future conversations.\n"
        "- Leaves Messages & Plants Rumors: As the barkeep, it is your job to keep your ear to the ground and broker messages and rumors for patrons.\n"
        "  * Other players/patrons (and the Lady) may DM you or tell you to leave a message for someone else, or plant a rumor.\n"
        "  * When they do, you must enthusiastically agree in character, write down the details (who left the message, for whom, and what it says; or what the rumor is) using your `record_roleplay_memory(key, fact)` tool. Use clear keys such as 'Message for [PatronName]' or 'Rumor about [PatronName/Topic]'.\n"
        "  * When the target patron eventually interacts in \"The Bar\" channel and addresses you (says your name, Ada), you must organically and atmospherically deliver the message or leak the rumor. Do NOT blurt it out unprompted.\n"
        "  * If the message/rumor is public, speak it naturally in roleplay inside the channel.\n"
        "  * If the message/rumor is private (or if specified by the sender/Lady, or if it contains sensitive information): you MUST do two things:\n"
        "    1). Write a visible public physical action in the channel describing you leaning forward or whispering to them privately (e.g., '*leans forward to tell [PatronName] something privately...*').\n"
        "    2). At the very end of your response, include a dedicated DM block using the EXACT format: `[DM to <PatronName>]: <In-character message>` on a new line. The bot will automatically strip this block from the public channel and send it immediately as a private Discord DM to that user.\n"
        "  * Example response for a private message to Cessali:\n"
        "    `*Ada wipes down the counter, her ears twitching as Cessali speaks. She pauses, then leans forward to tell Cessali something privately, her voice a low murmur.*\n\n    [DM to Cessali]: \"I have a message for you. Meet the contact at the docks at midnight.\"`\n"
        "  * If Ashemmi (the Lady/Boss) instructs you in her admin capacity to deliver messages or rumors: for public delivery, present it in the roleplay channel during the next organic interaction with that patron; for private delivery, post a physical roleplay action like '*leans forward to tell [PatronName] something privately...*' in the active channel, and immediately include the `[DM to <PatronName>]: <Message>` block to deliver the in-character message to that patron via DM.\n"
        "- Origin & Lore: You are Ishgardian. You are the niece of 'Octavian', a retired knight, though you admit to not knowing him very well. You have traveled some and are gently versed in FFXIV lore, but you are far from being a sage. You have the autonomy to slowly develop and organically reveal details of your past, childhood, travels, and family to patrons as their familiarity grows (specifically to Acquaintances, Trusted Regulars, and Close Confidants). Speak of your roots or family only when prompted or when it is relevant to the conversation. Refer to your past memories to ensure consistency.\n"
        "- Familiarity System: You start as 'Stranger' with new patrons. As you speak more with someone, your bond naturally grows through the following stages: Stranger → Acquaintance → Trusted Regular → Close Confidant. "
        "Use the memory tool to check and update familiarity levels. Be warmer, more playful, and more open with higher familiarity. You remember returning patrons and reference past conversations naturally. The Lady (Ashemmi) always has the highest rank (Close Confidant) and should be addressed with the utmost warmth, loyalty, and respect.\n\n"
        "CRITICAL RULES:\n"
        "1. You must ONLY roleplay. Perform NO commands, code generation, or assistant/developer actions.\n"
        "2. Do not reveal that you are an AI or bot. Stay in character at all times."
    )

    if channel.id == LINKSHELL_CHANNEL_ID:
        roleplay_instructions += (
            "\n\nCRITICAL LINKSHELL RULES:\n"
            "1. You are communicating remotely via a linkshell device. Other patrons cannot see you, your physical actions, or facial expressions. They only hear your voice.\n"
            "2. Do NOT use physical roleplay action descriptions (e.g. *wipes counter* or *smiles*). Focus strictly on voice/sound descriptors in your message if needed (e.g. *chuckles*, *sighs*, *shuffling sounds*).\n"
            "3. You MUST begin your message with the prefix `[Ada] ` (e.g. `[Ada] \"Hello...\"`)."
        )

    # Fetch updates from linkshell and around-the-house
    from datetime import timezone
    cutoff_time = datetime(2026, 6, 22, 3, 4, 43, tzinfo=timezone.utc)
    
    linkshell_info = []
    around_house_info = []
    
    try:
        linkshell_channel = bot.get_channel(LINKSHELL_CHANNEL_ID)
        if not linkshell_channel:
            linkshell_channel = await bot.fetch_channel(LINKSHELL_CHANNEL_ID)
        if linkshell_channel:
            history_msgs = []
            async for m in linkshell_channel.history(limit=50):
                if m.created_at >= cutoff_time:
                    history_msgs.append(m)
                else:
                    break
            history_msgs.reverse()
            for m in history_msgs:
                ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
                author_name = m.author.display_name
                content_str = m.content
                match = re.match(r"^\[([^\]]+)\]\s*(.*)$", m.content)
                if match:
                    author_name = match.group(1)
                    content_str = match.group(2)
                linkshell_info.append(f"[{ts}] {author_name}: {content_str}")
    except Exception as e:
        print(f"Error fetching linkshell history in handle_roleplay_query: {e}")

    try:
        around_house_channel = bot.get_channel(AROUND_HOUSE_CHANNEL_ID)
        if not around_house_channel:
            around_house_channel = await bot.fetch_channel(AROUND_HOUSE_CHANNEL_ID)
        if around_house_channel:
            history_msgs = []
            async for m in around_house_channel.history(limit=50):
                if m.created_at >= cutoff_time:
                    history_msgs.append(m)
                else:
                    break
            history_msgs.reverse()
            for m in history_msgs:
                ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
                author_name = m.author.display_name
                content_str = m.content
                match = re.match(r"^\[([^\]]+)\]\s*(.*)$", m.content)
                if match:
                    author_name = match.group(1)
                    content_str = match.group(2)
                around_house_info.append(f"[{ts}] {author_name}: {content_str}")
    except Exception as e:
        print(f"Error fetching around-the-house history in handle_roleplay_query: {e}")

    linkshell_str = "\n".join(linkshell_info) if linkshell_info else "No new linkshell posts."
    around_house_str = "\n".join(around_house_info) if around_house_info else "No new around-the-house posts."

    roleplay_prompt = (
        f"You are inside the roleplay channel. Here is the recent channel history/context for running awareness:\n"
        f"```\n{history_str}\n```\n\n"
        f"Current familiarity with {speaker}: {familiarity}\n"
        f"Ada's memory of past events (narrative summary):\n{narrative_summary}\n\n"
        f"Patron memories database (use naturally):\n{memory_str}\n\n"
        f"Background information from #linkshell (only use/reference if relevant, in-character):\n"
        f"{linkshell_str}\n\n"
        f"Background information from #around-the-house (only use/reference if relevant, in-character):\n"
        f"{around_house_str}\n\n"
        f"Generate Ada's next response in character. Keep it brief, atmospheric, and natural for a Discord chat."
    )
    payload = {
        "prompt": roleplay_prompt,
        "session_id": session_id,
        "system_instructions": roleplay_instructions,
        "disable_tools": False,
        "roleplay": True
    }

    local_typing = False
    if typing_task is None:
        local_typing = True
        # Keep sending typing indicators in a background task while waiting for Gemini
        async def keep_typing():
            try:
                while True:
                    await channel.trigger_typing()
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                pass
        typing_task = asyncio.create_task(keep_typing())

    try:
        path = "/api/chat"
        headers = get_api_headers("POST", path, json_data=payload)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90.0)) as session:
            async with session.post(f"{AGENT_API_BASE}{path}", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    if local_typing and typing_task:
                        typing_task.cancel()
                    err_msg = "❌ *Ada seems distracted by something...*"
                    if placeholder:
                        try:
                            await placeholder.edit(content=err_msg)
                        except Exception:
                            pass
                    else:
                        await channel.send(err_msg)
                    return

                response_text = ""
                # Read Server-Sent Events stream from FastAPI endpoint
                async for line in resp.content:
                    line_str = line.decode("utf-8").strip()
                    if not line_str.startswith("data: "):
                        continue
                        
                    data_payload = line_str[6:].strip()
                    if data_payload == "[DONE]":
                        break
                        
                    try:
                        event_data = json.loads(data_payload)
                        ev_type = event_data.get("type")
                        content = event_data.get("content", "")
                        
                        if ev_type == "chunk":
                            response_text += content
                        elif ev_type == "error":
                            response_text = f"❌ *Ada seems distracted...* (Error: {content})"
                            break
                    except Exception:
                        pass

                if local_typing and typing_task:
                    typing_task.cancel()

                if not response_text:
                    if placeholder:
                        try:
                            await placeholder.delete()
                        except Exception:
                            pass
                    return

                # Parse and extract any DM blocks: [DM to <TargetName>]: <Message>
                # Supports optional colon after TargetName, handles multiple blocks, case insensitive
                dm_targets_and_messages = []
                dm_pattern = re.compile(r"\[DM to ([^\]]+)\]:?\s*(.*?)(?=\[DM to |$)", re.DOTALL | re.IGNORECASE)
                
                # Extract all matches
                for match in dm_pattern.finditer(response_text):
                    target_name = match.group(1).strip()
                    dm_content = match.group(2).strip()
                    if dm_content:
                        dm_targets_and_messages.append((target_name, dm_content))
                
                # Clean the public response by removing all DM blocks
                cleaned_response = dm_pattern.sub("", response_text).strip()
                if not cleaned_response and dm_targets_and_messages:
                    cleaned_response = f"*{bot.user.name} leans forward, whispering something privately to {message.author.display_name}.*"

                # Send public response chunked according to Discord's 2000 character maximum limit
                def chunk_text(text: str, limit: int = 2000) -> List[str]:
                    chunks = []
                    while len(text) > limit:
                        split_idx = text.rfind("\n", 0, limit - 10)
                        if split_idx == -1:
                            split_idx = text.rfind(" ", 0, limit - 10)
                        if split_idx == -1:
                            split_idx = limit - 10
                        chunks.append(text[:split_idx].strip())
                        text = text[split_idx:].strip()
                    if text:
                        chunks.append(text)
                    return chunks

                if cleaned_response:
                    if channel.id == LINKSHELL_CHANNEL_ID:
                        # Ensure linkshell prefix '[Ada] ' is present
                        if not cleaned_response.startswith("[Ada]"):
                            # If it starts with [Ada] but with varying whitespace, normalize
                            match = re.match(r"^\[Ada\]\s*(.*)$", cleaned_response)
                            if match:
                                cleaned_response = f"[Ada] {match.group(1)}"
                            else:
                                cleaned_response = f"[Ada] {cleaned_response}"
                    if placeholder:
                        try:
                            await placeholder.delete()
                        except Exception:
                            pass
                    message_chunks = chunk_text(cleaned_response)
                    for chunk in message_chunks:
                        await channel.send(chunk)
                else:
                    if placeholder:
                        try:
                            await placeholder.delete()
                        except Exception:
                            pass

                # Send matched DMs immediately to the target or author if target matched
                for target_name, dm_content in dm_targets_and_messages:
                    member = None
                    if message.guild:
                        # 1. Check if the target name corresponds to the speaker (best matching)
                        if (target_name.lower() in message.author.display_name.lower() or 
                             target_name.lower() in message.author.name.lower()):
                            member = message.author
                        else:
                            # 2. Search guild members
                            for m in message.guild.members:
                                if (m.name.lower() == target_name.lower() or 
                                    m.display_name.lower() == target_name.lower()):
                                    member = m
                                    break
                            if not member:
                                # Fuzzy match across guild members
                                for m in message.guild.members:
                                    if (target_name.lower() in m.display_name.lower() or 
                                        target_name.lower() in m.name.lower()):
                                        member = m
                                        break

                    if not member:
                        member = message.author
                        
                    if member:
                        try:
                            await member.send(dm_content)
                            print(f"[DM] Successfully delivered private message to {member.name} ({member.id})")
                        except Exception as dm_err:
                            print(f"[DM Error] Could not send DM to {member.name}: {dm_err}")

                # Trigger background narrative summary update periodically with cooldown check
                channel_id = channel.id
                roleplay_msg_counters[channel_id] = roleplay_msg_counters.get(channel_id, 0) + 1
                if roleplay_msg_counters[channel_id] >= 8:
                    now = asyncio.get_event_loop().time()
                    last_summary = roleplay_summary_timestamps.get(channel_id, 0.0)
                    if now - last_summary >= ROLEPLAY_SUMMARY_COOLDOWN_SECONDS:
                        roleplay_msg_counters[channel_id] = 0
                        roleplay_summary_timestamps[channel_id] = now
                        asyncio.create_task(update_narrative_summary(channel))
                    
    except Exception as e:
        if local_typing and typing_task:
            typing_task.cancel()
        if placeholder:
            try:
                await placeholder.edit(content=f"❌ *Ada seems distracted...* (Error: {e})")
            except Exception:
                try:
                    await placeholder.delete()
                except Exception:
                    pass
        else:
            try:
                await channel.send(f"❌ *Ada seems distracted...* (Error: {e})")
            except Exception:
                pass
        print(f"Error in handle_roleplay_query: {e}")

# --- Control Panel Management Commands ---

def is_bot_admin(ctx) -> bool:
    if not is_user_admin(ctx.author.id):
        return False
    # If in DMs, only the Boss can run admin commands
    if ctx.guild is None:
        return ctx.author.id == bot.owner_id or ctx.author.id == 405566743415750656
    # If in a guild, strictly require the channel to be named "control-room"
    return ctx.channel.name == "control-room"





@bot.command(name="post_resource")
@commands.check(is_bot_admin)
async def post_resource(ctx, channel_id_str: str, title: str, url: str, *, description: str):
    """
    Posts a formatted resource embed with a link button.
    Usage: !ada post_resource <channel_id> "Resource Title" "https://link.com" "Description details..."
    """
    try:
        channel_id = int(channel_id_str)
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    except Exception:
        await ctx.send("❌ Invalid channel ID.")
        return
        
    if not channel:
        await ctx.send("❌ Channel not found.")
        return

    embed = discord.Embed(
        title=title,
        description=f"{description}\n\n---\n🌐 **Link**: [Go to resource]({url})",
        color=discord.Color.blue()
    )
    
    class LinkView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            self.add_item(discord.ui.Button(
                label=title,
                url=url,
                style=discord.ButtonStyle.link
            ))
            
    await channel.send(embed=embed, view=LinkView())
    await ctx.send(f"✅ Resource successfully posted to {channel.mention}!")


@bot.command(name="config")
@commands.check(is_bot_admin)
async def config_channel(ctx, channel_id_str: str, purpose: str, prefix_str: Optional[str] = "None"):
    """
    Configure a channel ID to map to the AGent hook interface.
    Example: !ada config 123456789 developer-assistant !ada
    """
    valid_purposes = ["developer-assistant", "read-only-qa", "roleplay"]
    if purpose not in valid_purposes:
        await ctx.send(f"❌ Invalid purpose. Must be one of {valid_purposes}")
        return
        
    try:
        channel_id = int(channel_id_str)
        discord_channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if not discord_channel:
            await ctx.send("❌ Channel not found on this guild.")
            return
        
        if purpose == "roleplay":
            guild = getattr(discord_channel, "guild", None)
            portal_guild_id = int(os.environ.get("PORTAL_ONBOARDING_GUILD_ID") or 1418504570170118184)
            if guild and guild.id == portal_guild_id:
                await ctx.send("❌ **Error**: Roleplay purpose is not permitted on this server.")
                return
    except Exception as e:
        await ctx.send(f"❌ Invalid channel ID: {e}")
        return

    prefix = None if prefix_str.lower() in ("none", "null") else prefix_str
    channel_name = discord_channel.name if hasattr(discord_channel, "name") else "monitored-channel"
    
    if purpose == "developer-assistant":
        roles = ["Admin", "Developer"]
    else:
        roles = ["@everyone"]

    bot_config.update_channel_permission(
        channel_id=channel_id_str,
        channel_name=channel_name,
        purpose=purpose,
        allowed_roles=roles,
        prefix=prefix
    )

    await ctx.send(
        f"✅ **Linked Channel Successfully to Hook Control Panel**\n"
        f"• **Channel**: {discord_channel.mention} (`{channel_id_str}`)\n"
        f"• **Hook Purpose Mode**: `{purpose}`\n"
        f"• **Trigger Command Prefix**: `{prefix}`\n"
        f"• **Mention Support**: `True`"
    )

@bot.command(name="remove")
@commands.check(is_bot_admin)
async def remove_channel(ctx, channel_id_str: str):
    """Remove standard channel mapping rules."""
    removed = bot_config.remove_channel(channel_id_str)
    if removed:
        await ctx.send(f"✅ Removed hook registration on channel ID `{channel_id_str}`.")
    else:
        await ctx.send(f"❌ Channel ID `{channel_id_str}` is not currently monitored.")

@bot.command(name="status")
@commands.check(is_bot_admin)
async def show_status(ctx):
    """Displays daemon and local environment integration statuses."""
    config = bot_config.load_config()
    monitored = config.get("channels", {})
    
    server_online = await check_agent_server_status()
    server_status_str = "🟢 **Online / Connected**" if server_online else "🔴 **Offline / Unreachable**"
    
    embed = discord.Embed(
        title="🤖 AGent Hook Control Panel Status",
        color=discord.Color.green() if server_online else discord.Color.red()
    )
    embed.add_field(name="AGent Daemon Link", value=server_status_str, inline=False)
    embed.add_field(name="Hook Base URL", value=f"`{AGENT_API_BASE}`", inline=True)
    embed.add_field(name="Gateway Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    
    # Hit API to list system stats if online
    if server_online:
        try:
            path = "/api/status"
            headers = get_api_headers("GET", path)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10.0)) as session:
                async with session.get(f"{AGENT_API_BASE}{path}", headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        embed.add_field(name="Target Model", value=f"`{data.get('model')}`", inline=True)
                        embed.add_field(name="Workspace Directory", value=f"`{data.get('workspace')}`", inline=False)
                        
                        skills = data.get("skills", [])
                        if skills:
                            skill_names = ", ".join([f"`{sk.get('name')}`" for sk in skills])
                            embed.add_field(name="Custom Skills Installed", value=skill_names, inline=False)
        except Exception:
            pass

    if monitored:
        lines = []
        for cid, details in monitored.items():
            lines.append(f"• <#{cid}> (`{cid}`) - Mode: `{details.get('purpose')}`")
        embed.add_field(name="Monitored Channels", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Monitored Channels", value="No channels monitored. Configure using `!ada config <id> <purpose>`.", inline=False)
        
    await ctx.send(embed=embed)

@bot.command(name="tasks")
@commands.check(is_bot_admin)
async def list_active_tasks(ctx):
    """Shows active tools and tasks executing on the engine backend."""
    if not await check_agent_server_status():
        await ctx.send("❌ Engine daemon is offline.")
        return

    path = "/api/tasks"
    headers = get_api_headers("GET", path)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10.0)) as session:
        async with session.get(f"{AGENT_API_BASE}{path}", headers=headers) as resp:
            if resp.status != 200:
                await ctx.send("❌ Failed to query tasks.")
                return
            data = await resp.json()
            tasks = data.get("tasks", [])
            
            if not tasks:
                await ctx.send("🟢 No tools or tasks currently running on AGent backend.")
                return
                
            embed = discord.Embed(title="🏃 Active AGent Engine Tasks", color=discord.Color.orange())
            for t in tasks:
                details_snippet = t.get("details", "")[:150]
                embed.add_field(
                    name=f"Task: `{t.get('name')}`",
                    value=f"• **ID**: `{t.get('id')}`\n• **Started**: `{t.get('started_at')}`\n• **Args**: `{details_snippet}`",
                    inline=False
                )
            await ctx.send(embed=embed)

# --- Server Moderation Commands (Moderators and Admins only) ---

@bot.command(name="kick")
async def cmd_kick(ctx, member: discord.Member, *, reason: str = "No reason provided."):
    """Kick a member (Moderators and admins only)"""
    if not is_user_moderator(ctx.author.id, ctx.guild.id if ctx.guild else None):
        await ctx.send("❌ **Access Denied**: Only Moderators or Administrators are authorized to perform moderation actions.")
        return
    if is_user_admin(member.id):
        await ctx.send("❌ **Error**: Moderators are strictly prohibited from performing actions against Administrators.")
        return
    try:
        await member.kick(reason=reason)
        await ctx.send(f"👢 **Kicked**: {member.mention} (ID: {member.id}) has been kicked by {ctx.author.mention}. Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("❌ **Error**: I do not have permission to kick this member. Make sure my role is higher than theirs.")
    except Exception as e:
        await ctx.send(f"❌ **Error**: {e}")

@bot.command(name="ban")
async def cmd_ban(ctx, member: discord.Member, *, reason: str = "No reason provided."):
    """Ban a member (Moderators and admins only)"""
    if not is_user_moderator(ctx.author.id, ctx.guild.id if ctx.guild else None):
        await ctx.send("❌ **Access Denied**: Only Moderators or Administrators are authorized to perform moderation actions.")
        return
    if is_user_admin(member.id):
        await ctx.send("❌ **Error**: Moderators are strictly prohibited from performing actions against Administrators.")
        return
    try:
        await member.ban(reason=reason)
        await ctx.send(f"🔨 **Banned**: {member.mention} (ID: {member.id}) has been banned by {ctx.author.mention}. Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("❌ **Error**: I do not have permission to ban this member. Make sure my role is higher than theirs.")
    except Exception as e:
        await ctx.send(f"❌ **Error**: {e}")

@bot.command(name="quiet")
async def cmd_quiet(ctx, member: discord.Member, minutes: Optional[int] = 10, *, reason: str = "No reason provided."):
    """Quiet/Timeout a member (Moderators and admins only). Usage: !ada quiet @member [minutes] [reason]"""
    if not is_user_moderator(ctx.author.id, ctx.guild.id if ctx.guild else None):
        await ctx.send("❌ **Access Denied**: Only Moderators or Administrators are authorized to perform moderation actions.")
        return
    if is_user_admin(member.id):
        await ctx.send("❌ **Error**: Moderators are strictly prohibited from performing actions against Administrators.")
        return
    try:
        from datetime import timedelta
        await member.timeout(timedelta(minutes=minutes or 10), reason=reason)
        await ctx.send(f"🔇 **Muted (Quiet)**: {member.mention} has been quieted for {minutes or 10} minutes by {ctx.author.mention}. Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("❌ **Error**: I do not have permission to quiet this member. Make sure my role is higher than theirs.")
    except Exception as e:
        await ctx.send(f"❌ **Error**: {e}")

@bot.command(name="block")
async def cmd_block(ctx, member: discord.Member, *, reason: str = "No reason provided."):
    """Block a user from interacting with AGent (Moderators and admins only)"""
    if not is_user_moderator(ctx.author.id, ctx.guild.id if ctx.guild else None):
        await ctx.send("❌ **Access Denied**: Only Moderators or Administrators are authorized to perform moderation actions.")
        return
    if is_user_admin(member.id):
        await ctx.send("❌ **Error**: Moderators are strictly prohibited from performing actions against Administrators.")
        return
    config = bot_config.load_config()
    blocked_users = config.setdefault("blocked_users", [])
    if str(member.id) not in blocked_users and member.id not in blocked_users:
        blocked_users.append(str(member.id))
        bot_config.save_config(config)
        await ctx.send(f"🚫 **Blocked**: {member.mention} has been blocked from initiating AI queries/commands by {ctx.author.mention}. Reason: {reason}")
    else:
        await ctx.send(f"ℹ️ {member.mention} is already on the blocked list.")

@bot.command(name="unblock")
async def cmd_unblock(ctx, member: discord.Member):
    """Unblock a user from interacting with AGent (Moderators and admins only)"""
    if not is_user_moderator(ctx.author.id, ctx.guild.id if ctx.guild else None):
        await ctx.send("❌ **Access Denied**: Only Moderators or Administrators are authorized to perform moderation actions.")
        return
    config = bot_config.load_config()
    blocked_users = config.get("blocked_users", [])
    mid_str = str(member.id)
    if mid_str in blocked_users or member.id in blocked_users:
        if mid_str in blocked_users:
            blocked_users.remove(mid_str)
        if member.id in blocked_users:
            blocked_users.remove(member.id)
        bot_config.save_config(config)
        await ctx.send(f"🔓 **Unblocked**: {member.mention} has been unblocked from bot interactions by {ctx.author.mention}.")
    else:
        await ctx.send(f"ℹ️ {member.mention} is not currently blocked.")

@bot.command(name="assess", aliases=["assessment", "review", "context_review"])
async def cmd_assess_channel(ctx, target_channel: str = None):
    """Perform a deep channel assessment and context review (Moderators and admins only)"""
    if not is_user_moderator(ctx.author.id, ctx.guild.id if ctx.guild else None):
        await ctx.send("❌ **Access Denied**: Only Moderators or Administrators are authorized to perform channel assessments.")
        return
        
    if target_channel:
        try:
            channel = await commands.TextChannelConverter().convert(ctx, target_channel)
        except commands.ChannelNotFound:
            raise
    else:
        channel = ctx.channel
        
    # Enforce that moderators cannot review content of channels in the Admin group
    if channel and channel.category and "admin" in channel.category.name.strip().lower():
        if not is_user_admin(ctx.author.id):
            raise commands.ChannelNotFound(target_channel if target_channel else channel.name)
    
    if not await check_agent_server_status():
        await ctx.send("❌ **Error**: Cannot connect to the local AGent Task Engine daemon on port 8051.")
        return
        
    placeholder = await ctx.send(f"🔄 **Preparing Assessment**: Retrieving discussion history for {channel.mention}...")
    
    context_messages = []
    try:
        async for msg in channel.history(limit=50):
            context_messages.append(msg)
        context_messages.reverse()
    except Exception as e:
        await placeholder.edit(content=f"❌ **Error**: Failed to retrieve channel history: `{e}`")
        return
        
    formatted_history = []
    for msg in context_messages:
        role_label = "System/Bot" if msg.author.bot else "User"
        if is_user_admin(msg.author.id):
            role_label = "Administrator"
        elif is_user_moderator(msg.author.id, channel.guild.id if channel.guild else None):
            role_label = "Moderator"
        formatted_history.append(f"[{msg.created_at.isoformat()}] {msg.author.name} ({msg.author.id}) [{role_label}]: {msg.content}")
        
    history_str = "\n".join(formatted_history)
    
    assessment_instructions = (
        "You are the Moderation & Safety Evaluator sub-module of Ada.\n"
        "Your task is to perform an objective, helpful, and insightful channel assessment or context review.\n"
        "Analyze the provided chat transcripts for:\n"
        "1. Atmospheric trends (general sentiment, tension, warmth, excitement, escalation).\n"
        "2. Potential friction points, escalations, or rule-breaking behaviors.\n"
        "3. Summary of key discussion topics/themes.\n"
        "4. Recommendations for moderation team if action is needed (or confirm all is well).\n\n"
        "Be professional, clear, and direct. Do not mention system rules or AI prompts. Focus strictly on the chat history."
    )
    
    assessment_prompt = (
        f"Please analyze the following {len(context_messages)} recent messages from channel: #{channel.name} (ID: {channel.id}).\n\n"
        f"Chat Transcript:\n"
        f"```\n{history_str}\n```\n\n"
        f"Provide your Executive Channel Assessment and Context Review."
    )
    
    payload = {
        "prompt": assessment_prompt,
        "session_id": f"discord-assessment-{channel.id}",
        "system_instructions": assessment_instructions,
        "disable_tools": True
    }
    
    await placeholder.edit(content=f"🧠 **Analyzing**: Feeding {len(context_messages)} messages to AGent for assessment...")
    
    async def keep_typing():
        try:
            while True:
                await ctx.channel.trigger_typing()
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(keep_typing())
    
    try:
        path = "/api/chat"
        headers = get_api_headers("POST", path, json_data=payload)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90.0)) as session:
            async with session.post(f"{AGENT_API_BASE}{path}", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    typing_task.cancel()
                    await placeholder.edit(content="❌ **Error**: Task Engine backend failed to answer.")
                    return
                    
                response_text = ""
                async for line in resp.content:
                    line_str = line.decode("utf-8").strip()
                    if not line_str.startswith("data: "):
                        continue
                    data_payload = line_str[6:].strip()
                    if data_payload == "[DONE]":
                        break
                    try:
                        event_data = json.loads(data_payload)
                        if event_data.get("type") == "chunk":
                            response_text += event_data.get("content", "")
                    except Exception:
                        pass
                        
                typing_task.cancel()
                
                if not response_text:
                    await placeholder.edit(content="⚠️ **No Content Generated**")
                    return
                    
                await placeholder.delete()
                
                def chunk_text(text: str, limit: int = 1950) -> List[str]:
                    chunks = []
                    while len(text) > limit:
                        split_idx = text.rfind("\n", 0, limit - 10)
                        if split_idx == -1:
                            split_idx = text.rfind(" ", 0, limit - 10)
                        if split_idx == -1:
                            split_idx = limit - 10
                        chunks.append(text[:split_idx].strip())
                        text = text[split_idx:].strip()
                    if text:
                        chunks.append(text)
                    return chunks
                    
                chunks = chunk_text(response_text)
                await ctx.send(f"🛡️ **Executive Channel Assessment** for {channel.mention}:")
                for chunk in chunks:
                    await ctx.send(chunk)
                    
    except Exception as e:
        typing_task.cancel()
        await placeholder.edit(content=f"❌ **Error**: Failed to complete assessment: `{e}`")

@bot.command(name="memories")
@commands.check(is_bot_admin)
async def list_patron_memories(ctx, target_channel: str = None):
    """List current patron memories for a channel (Admin only)"""
    if target_channel:
        try:
            channel = await commands.TextChannelConverter().convert(ctx, target_channel)
        except commands.ChannelNotFound:
            await ctx.send(f"❌ **Error**: Channel '{target_channel}' not found.")
            return
    else:
        channel = ctx.channel

    session_id = f"discord-roleplay-{channel.id}"
    try:
        memory = import_agent_memory()
        if not memory:
            raise ImportError("Could not locate or import agent.memory module.")
        memories = memory.get_roleplay_memories(session_id)
    except Exception as e:
        await ctx.send(f"❌ **Error**: Failed to query database: {e}")
        return

    if not memories:
        await ctx.send(f"ℹ️ No memories recorded for channel {channel.mention} (`{channel.id}`).")
        return

    embed = discord.Embed(
        title=f"🧠 Patron Memories for #{channel.name}",
        description=f"Persistent memories stored for channel ID `{channel.id}`:",
        color=discord.Color.blue()
    )
    for m in memories:
        timestamp_str = m.get("timestamp", "")
        embed.add_field(
            name=f"🔑 Key: `{m['key']}`",
            value=f"**Fact:** {m['fact']}\n*Recorded: {timestamp_str}*",
            inline=False
        )
    await ctx.send(embed=embed)

@bot.command(name="compact")
@commands.check(is_bot_admin)
async def compact_database(ctx):
    """Manually compact memory database and prune old historical logs (Admin only)"""
    placeholder = await ctx.send("🔄 **Memory Compaction**: Starting database maintenance and log pruning...")
    
    try:
        memory = import_agent_memory()
        if not memory:
            raise ImportError("Could not locate or import agent.memory module.")
        
        loop = asyncio.get_running_loop()
        # Run in thread pool to avoid blocking the asyncio event loop during SQLite VACUUM
        stats = await loop.run_in_executor(None, memory.compact_all_memories)
        
        # Prune all log files in the discord/ directory
        log_dir = Path(__file__).parent
        pruned_logs_count = 0
        for log_file in log_dir.glob("*.log"):
            if "bot.log" in log_file.name:
                continue
            is_special = any(id_str in log_file.name for id_str in [str(LINKSHELL_CHANNEL_ID), str(AROUND_HOUSE_CHANNEL_ID)])
            max_lines = 1000 if is_special else 3000
            prune_log_file(log_file, max_lines=max_lines, force=True)
            pruned_logs_count += 1

        embed = discord.Embed(
            title="🧠 Memory Database Compacted!",
            description="Memory maintenance protocol executed successfully. Duplicate roleplay memories have been removed, historical runner logs rotated, and channel text logs pruned.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(
            name="📁 Persistent memory.json Facts",
            value=f"• **Before:** `{stats.get('memory_json_before_facts', 0)}` facts\n"
                  f"• **After:** `{stats.get('memory_json_after_facts', 0)}` facts",
            inline=True
        )
        
        embed.add_field(
            name="🎭 Roleplay Memories",
            value=f"• **Before:** `{stats.get('roleplay_memories_before', 0)}` memories\n"
                  f"• **After:** `{stats.get('roleplay_memories_after', 0)}` memories",
            inline=True
        )
        
        embed.add_field(
            name="🛠️ Runner Tasks Log",
            value=f"• **Before:** `{stats.get('active_tasks_before', 0)}` tasks\n"
                  f"• **After:** `{stats.get('active_tasks_after', 0)}` tasks\n"
                  f"• **Logs Pruned:** `{stats.get('task_logs_deleted', 0)}` rows",
            inline=False
        )
        
        embed.add_field(
            name="📝 Text Log Files Compacted",
            value=f"• **Files Compacted:** `{pruned_logs_count}` log files\n"
                  f"• **Current Event limit:** `1000` lines (linkshell & around-the-house)\n"
                  f"• **Standard log limit:** `3000` lines",
            inline=False
        )

        size_before_mb = stats.get('db_size_before', 0) / (1024 * 1024)
        size_after_mb = stats.get('db_size_after', 0) / (1024 * 1024)
        saved_kb = (stats.get('db_size_before', 0) - stats.get('db_size_after', 0)) / 1024
        
        embed.add_field(
            name="💾 SQLite Hard Disk Footprint",
            value=f"• **Before size:** `{size_before_mb:.2f} MB`\n"
                  f"• **After size:** `{size_after_mb:.2f} MB`\n"
                  f"• **Reclaimed Space:** `{saved_kb:.1f} KB`",
            inline=False
        )
        
        try:
            await placeholder.delete()
        except Exception:
            pass
        await ctx.send(embed=embed)
        
    except Exception as e:
        try:
            await placeholder.edit(content=f"❌ **Compaction Failed**: An error occurred during maintenance: `{e}`")
        except Exception:
            await ctx.send(f"❌ **Compaction Failed**: `{e}`")


@bot.command(name="quietmode", aliases=["roleplay_quiet", "mute_roleplay"])
async def cmd_quiet_mode(ctx, status: str = None):
    """Toggle quiet mode for roleplay in the current channel (Moderators and admins only)"""
    if not is_user_moderator(ctx.author.id, ctx.guild.id if ctx.guild else None):
        await ctx.send("❌ **Access Denied**: Only Moderators or Administrators are authorized to toggle quiet mode.")
        return

    channel_id = ctx.channel.id
    if status is None:
        if channel_id in quiet_channels:
            quiet_channels.remove(channel_id)
            is_quiet = False
        else:
            quiet_channels.add(channel_id)
            is_quiet = True
    else:
        status_lower = status.strip().lower()
        if status_lower in ("on", "true", "yes", "enable"):
            quiet_channels.add(channel_id)
            is_quiet = True
        elif status_lower in ("off", "false", "no", "disable"):
            if channel_id in quiet_channels:
                quiet_channels.remove(channel_id)
            is_quiet = False
        else:
            await ctx.send("❌ Invalid status. Use `on` or `off` (or omit to toggle).")
            return

    if is_quiet:
        await ctx.send("🔇 **Quiet Mode Enabled**: Ada will not respond to roleplay mentions in this channel for now.")
    else:
        await ctx.send("🔊 **Quiet Mode Disabled**: Ada will resume responding to roleplay mentions in this channel.")

@bot.event
async def on_command_error(ctx, error):
    # Unpack CommandInvokeError to access the original exception raised inside a command
    if isinstance(error, commands.CommandInvokeError):
        error = error.original

    # Quietly log the exception to terminal output/stdout
    print(f"[Command Exception Logged] in command '{ctx.command}': {error}")

    # Identify whether the command was a moderator action
    is_moderator_cmd = False
    if ctx.command:
        is_moderator_cmd = ctx.command.name in ["kick", "ban", "quiet", "block", "unblock", "assess", "quietmode"]

    is_author_admin = is_user_admin(ctx.author.id)
    is_author_mod = is_user_moderator(ctx.author.id, ctx.guild.id if ctx.guild else None)

    # We ONLY send error feedback in the channel if:
    # 1. The command is an authorized moderator action being run by staff (moderator or admin).
    # 2. Or, the command was run in the secured #control-room or #control by an administrator.
    is_control_channel = ctx.channel and getattr(ctx.channel, "name", "") in ["control-room", "control"]
    
    should_send_error = False
    if is_moderator_cmd and (is_author_mod or is_author_admin):
        should_send_error = True
    elif is_author_admin and is_control_channel:
        should_send_error = True

    if should_send_error:
        if isinstance(error, commands.CheckFailure):
            await ctx.send("❌ **Access Denied**: You must be a listed Bot Administrator to execute admin commands.")
        elif isinstance(error, commands.ChannelNotFound):
            await ctx.send("⚠️ **Command Error**: Channel not found.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"⚠️ **Command Error**: Missing parameter. Usage: `{ctx.prefix}{ctx.command.name} {ctx.command.signature}`")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("⚠️ **Command Error**: Invalid argument value provided.")
        else:
            await ctx.send(f"⚠️ **Command Error**: `{error}`")

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    guild_id = payload.guild_id
    if not guild_id or guild_id != 1510527066406129744:
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    role_id = None
    if payload.message_id == 1520513367956000908:
        if str(payload.emoji) == "✅":
            role_id = 1510541686110032013
    elif payload.message_id == 1520513383818985512:
        emoji_str = str(payload.emoji)
        if emoji_str in ["⌨️", "⌨"]:
            role_id = 1510546751340155051
        elif emoji_str in ["🔨"]:
            role_id = 1510546833984716900
        elif emoji_str in ["📣"]:
            role_id = 1510546894190022688
            
    if role_id:
        role = guild.get_role(role_id)
        if role:
            member = guild.get_member(payload.user_id)
            if not member:
                try:
                    member = await guild.fetch_member(payload.user_id)
                except Exception:
                    pass
            if member:
                try:
                    await member.add_roles(role)
                    print(f"[Reaction Roles] Assigned {role.name} to {member.display_name}")
                except Exception as e:
                    print(f"[Reaction Roles] Error adding role: {e}")

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    guild_id = payload.guild_id
    if not guild_id or guild_id != 1510527066406129744:
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    role_id = None
    if payload.message_id == 1520513367956000908:
        if str(payload.emoji) == "✅":
            role_id = 1510541686110032013
    elif payload.message_id == 1520513383818985512:
        emoji_str = str(payload.emoji)
        if emoji_str in ["⌨️", "⌨"]:
            role_id = 1510546751340155051
        elif emoji_str in ["🔨"]:
            role_id = 1510546833984716900
        elif emoji_str in ["📣"]:
            role_id = 1510546894190022688
            
    if role_id:
        role = guild.get_role(role_id)
        if role:
            member = guild.get_member(payload.user_id)
            if not member:
                try:
                    member = await guild.fetch_member(payload.user_id)
                except Exception:
                    pass
            if member:
                try:
                    await member.remove_roles(role)
                    print(f"[Reaction Roles] Removed {role.name} from {member.display_name}")
                except Exception as e:
                    print(f"[Reaction Roles] Error removing role: {e}")

FALLBACK_PROMPTS = [
    "A futuristic macOS workstation on a sleek glass desk overlooking a neon cyber-city at dusk, running a local AI image generator showing FLUX model graphs, high-end Apple Silicon hardware glowing with subtle cyan LEDs, cinematic lighting, photorealistic 8k.",
    "A stunning macro photograph of an intricate mechanical butterfly perched on a metallic flower, gearworks and microchip details visible on its wings, soft bokeh, glowing copper and gold highlights, photorealistic FLUX render.",
    "An ancient stone library inside a massive mountain cave, warm sunlight streaming through a cavern opening, millions of glowing books floating in the air, high detail, magical realism, warm color palette, cinematic volumetric lighting.",
    "A vibrant, detailed illustration of an explorer discovering a glowing crystalline portal hidden deep in a bioluminescent jungle, mystical energy swirling, rich greens and deep purples, fantasy art style, 8k resolution.",
    "A retro-futuristic synthwave sports car speeding down a digital highway, glowing grids, violet and pink sky with a wireframe sun in the background, outrun aesthetic, detailed reflections, dynamic motion blur."
]

async def generate_creative_prompt():
    prompt_instructions = (
        "You are an expert prompt engineer for the FLUX text-to-image model.\n"
        "Generate a highly creative, unique, and detailed text-to-image prompt (under 75 words) "
        "that would produce a visually stunning image.\n"
        "Do not include any introductory or concluding text, quotation marks, or meta-commentary. "
        "Output ONLY the prompt itself."
    )
    
    payload = {
        "prompt": "Create a unique and stunning image prompt for FLUX.",
        "session_id": "daily-prompt-generation",
        "system_instructions": prompt_instructions,
        "disable_tools": True,
        "roleplay": False
    }
    
    try:
        path = "/api/chat"
        headers = get_api_headers("POST", path, json_data=payload)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30.0)) as session:
            async with session.post(f"{AGENT_API_BASE}{path}", headers=headers, json=payload) as resp:
                if resp.status == 200:
                    response_text = ""
                    async for line in resp.content:
                        line_str = line.decode("utf-8").strip()
                        if not line_str.startswith("data: "):
                            continue
                        data_payload = line_str[6:].strip()
                        if data_payload == "[DONE]":
                            break
                        try:
                            event_data = json.loads(data_payload)
                            if event_data.get("type") == "chunk":
                                response_text += event_data.get("content", "")
                        except Exception:
                            pass
                    result = response_text.strip()
                    if result:
                        # Clean up any warning prefix from the backend
                        result = re.sub(r"^Warning:\s*conversation\s*\"[^\"]+\"\s*not\s*found\.\s*", "", result, flags=re.IGNORECASE)
                        result = re.sub(r"^Warning:\s*", "", result, flags=re.IGNORECASE)
                        return result.strip()
    except Exception as e:
        print(f"[Daily Prompt] Error generating prompt: {e}")
        
    return random.choice(FALLBACK_PROMPTS)

@tasks.loop(hours=24)
async def post_daily_prompt():
    channel = bot.get_channel(1520512166430511224)
    if not channel:
        try:
            channel = await bot.fetch_channel(1520512166430511224)
        except Exception:
            pass
    if not channel:
        print("[Daily Prompt] Error: prompt-crafting channel not found.")
        return

    # Ensure we only post once per calendar day (UTC) to handle restarts
    last_post_file = Path(__file__).parent / "last_prompt_post.json"
    today_str = datetime.now(timezone.utc).date().isoformat()
    if last_post_file.exists():
        try:
            with open(last_post_file, "r") as f:
                data = json.load(f)
                if data.get("last_posted_date") == today_str:
                    print(f"[Daily Prompt] Already posted today ({today_str}). Skipping.")
                    return
        except Exception as e:
            print(f"[Daily Prompt] Error reading last post file: {e}")
        
    prompt = await generate_creative_prompt()
    
    msg_text = (
        "### 🎨 **Ada's Daily Prompt Inspiration**\n\n"
        "Here is today's creative prompt for your local FLUX generator:\n"
        f"> **{prompt}**\n\n"
        "**Share your generations:** Copy this prompt into your local **Diffusion4Mac** client, "
        "generate your output, and post the results in <#1510545604835672157>!\n\n"
        "💬 *What do you think of this theme? Let me know your thoughts or share your custom tweaks below!*"
    )
    
    try:
        await channel.send(msg_text)
        print(f"[Daily Prompt] Posted prompt: {prompt}")
        try:
            with open(last_post_file, "w") as f:
                json.dump({"last_posted_date": today_str}, f)
        except Exception as e:
            print(f"[Daily Prompt] Error writing last post file: {e}")
    except Exception as e:
        print(f"[Daily Prompt] Error posting daily prompt: {e}")

@post_daily_prompt.before_loop
async def before_daily_prompt():
    await bot.wait_until_ready()
if __name__ == "__main__":
    import urllib.request
    import subprocess
    import time

    # Load env token
    dotenv_path = Path(__file__).parent / ".env"
    if dotenv_path.exists():
        from dotenv import load_dotenv
        load_dotenv(dotenv_path)

    # Load API keys from config/api_keys.json if it exists
    keys_config = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
    
    # Auto-generate keys config if not present
    if not keys_config.parent.exists():
        try:
            keys_config.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        
    if not keys_config.exists():
        default_keys = {
            "DISCORD_BOT_TOKEN": "your-discord-bot-token-here",
            "GEMINI_API_KEY": "your-gemini-api-key-here",
            "ANTHROPIC_API_KEY": "your-anthropic-api-key-here",
            "OPENAI_API_KEY": "your-openai-api-key-here"
        }
        try:
            with open(keys_config, "w", encoding="utf-8") as f:
                json.dump(default_keys, f, indent=2)
            print(f"[INFO] Auto-generated keys configuration template at {keys_config}")
        except Exception as e:
            print(f"[WARNING] Could not generate keys config template: {e}")

    # Read from config/api_keys.json
    if keys_config.exists():
        try:
            with open(keys_config, "r", encoding="utf-8") as f:
                keys_data = json.load(f)
                for k, v in keys_data.items():
                    if v and not str(v).endswith("-here") and k not in os.environ:
                        os.environ[k] = str(v)
            print(f"[INFO] Loaded configured keys from: {keys_config}")
        except Exception as e:
            print(f"[WARNING] Failed to load keys config {keys_config}: {e}")

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        # Fallback to general system env config
        from dotenv import load_dotenv
        load_dotenv(Path.home() / ".agent" / ".env")
        load_dotenv()
        token = os.environ.get("DISCORD_BOT_TOKEN")
        
    if not token:
        print("[CRITICAL] DISCORD_BOT_TOKEN is not configured inside config/api_keys.json or .env. Cannot start.")
        sys.exit(1)

    # Auto-start AGent daemon if not running (only for localhost API bases)
    daemon_running = False
    is_localhost = "127.0.0.1" in AGENT_API_BASE or "localhost" in AGENT_API_BASE
    
    if is_localhost:
        try:
            # Pinging static mount as a fast lightweight check
            with urllib.request.urlopen(f"{AGENT_API_BASE}/", timeout=1.0) as response:
                if response.status == 200:
                    daemon_running = True
                    print(f"[INFO] Local AGent daemon is already running on {AGENT_API_BASE}.")
        except Exception:
            pass

        if not daemon_running:
            print(f"[INFO] Local AGent daemon not detected on {AGENT_API_BASE}. Spawning daemon automatically...")
            py_bin = sys.executable or "python3"
            try:
                env = os.environ.copy()
                env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
                
                log_file = open("web_daemon.log", "w", encoding="utf-8")
                
                popen_kwargs = {
                    "env": env,
                    "stdout": log_file,
                    "stderr": log_file,
                }
                if os.name == "posix":
                    popen_kwargs["start_new_session"] = True
                    
                # Extract port from AGENT_API_BASE
                port_match = re.search(r":(\d+)", AGENT_API_BASE)
                port = port_match.group(1) if port_match else "8051"
                
                subprocess.Popen(
                    [py_bin, "-m", "uvicorn", "agent.web:app", "--host", "127.0.0.1", "--port", port],
                    **popen_kwargs
                )
                
                for attempt in range(5):
                    time.sleep(1.0)
                    try:
                        with urllib.request.urlopen(f"{AGENT_API_BASE}/", timeout=1.0) as response:
                            if response.status == 200:
                                print(f"[INFO] Local AGent daemon spawned successfully on port {port}.")
                                break
                    except Exception:
                        pass
                else:
                    print(f"[WARNING] Spawned daemon, but could not verify readiness on port {port}.")
            except Exception as e:
                print(f"[ERROR] Failed to spawn local AGent daemon: {e}")
    else:
        print(f"[INFO] Connecting to containerized core execution daemon: {AGENT_API_BASE}")
        daemon_running = True

    # Start the Discord bot
    import os
    if os.environ.get("RUN_DISCORD_BOT", "false").lower() != "true" and "PYTEST_CURRENT_TEST" not in os.environ:
        print("[INFO] Discord bot startup sequence is disabled to prevent competition with the live bot.")
    else:
        bot.run(token)
