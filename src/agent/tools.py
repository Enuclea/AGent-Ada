import os
import re
import json
from pathlib import Path
from typing import List, Optional

from agent import memory

SKILLS_DIR = Path.home() / ".agent" / "skills"
WORKSPACE_SKILLS_DIR = Path(os.getcwd()) / ".agents" / "skills"
OPENCLAW_EXTS_DIR = Path.home() / ".openclaw" / "extensions"
OPENCLAW_SKILLS_DIR = Path.home() / ".openclaw" / "skills"
HERMES_SKILLS_DIR = Path.home() / ".hermes" / "skills"

# Third-party / plugin registered tools list
PLUGIN_TOOLS = []

def register_plugin_tools(tools_list: list) -> None:
    """Dynamically registers tool functions from plugins."""
    global PLUGIN_TOOLS
    for t in tools_list:
        if t not in PLUGIN_TOOLS:
            PLUGIN_TOOLS.append(t)

def get_skills_paths() -> List[Path]:
    """Returns a list of directories containing custom skills or tools."""
    paths = [SKILLS_DIR]
    if OPENCLAW_EXTS_DIR.exists() and OPENCLAW_EXTS_DIR.is_dir():
        paths.append(OPENCLAW_EXTS_DIR)
    if OPENCLAW_SKILLS_DIR.exists() and OPENCLAW_SKILLS_DIR.is_dir():
        paths.append(OPENCLAW_SKILLS_DIR)
    if HERMES_SKILLS_DIR.exists() and HERMES_SKILLS_DIR.is_dir():
        paths.append(HERMES_SKILLS_DIR)
    return paths

def _parse_frontmatter(content: str) -> dict:
    """Helper to parse simple YAML-like frontmatter from SKILL.md."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}
    fm_text = match.group(1)
    result = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            result[k.strip().lower()] = v.strip()
    return result

def record_memory_fact(fact: str) -> str:
    """Records a new fact, note, or piece of knowledge about the user, project, or task.
    
    Use this to store important context that should be remembered across runs, such as
    user preferences, project details, build commands, or lessons learned.
    
    Args:
        fact: The text of the fact to record. E.g. "The build command is 'npm run build'" or "User prefers dark mode".
    """
    return memory.add_fact(fact)

def record_memory_key_value(key: str, value: str) -> str:
    """Stores or updates a key-value setting or preference in persistent memory.
    
    Args:
        key: The key identifier (e.g. 'user_name', 'project_status').
        value: The value to store associated with the key.
    """
    return memory.update_key_value(key, value)

def create_agent_skill(
    skill_name: str,
    description: str,
    instructions: str,
    script_content: Optional[str] = None,
    script_filename: Optional[str] = None,
) -> str:
    """Creates and installs a new custom skill/tool that will be loaded by AGent.
    
    Use this when you have successfully solved a problem, figured out a complex workflow,
    or built a helper script, and you want to register it as a reusable skill/tool
    for all future AGent runs.
    
    Args:
        skill_name: A short, hyphenated identifier for the skill (e.g. 'git-helper' or 'clean-code').
        description: A brief summary of what the skill does and when to use it.
        instructions: Detailed markdown instructions telling the agent how to execute the workflow.
        script_content: Optional python or bash code to save as a helper script inside the skill.
        script_filename: Optional filename of the script, required if script_content is provided (e.g. 'run.py' or 'check.sh').
    """
    # Normalize name to lowercase, alphanumeric and hyphens
    normalized_name = re.sub(r"[^a-z0-9\-]", "", skill_name.lower().replace(" ", "-"))
    if not normalized_name:
        return "Error: skill_name must contain alphanumeric characters or hyphens."
        
    skill_path = SKILLS_DIR / normalized_name
    skill_path.mkdir(parents=True, exist_ok=True)
    
    # Write SKILL.md with YAML frontmatter
    skill_md_content = f"""---
name: {normalized_name}
description: {description}
---

# {skill_name}

## Description
{description}

## Instructions
{instructions}
"""
    with open(skill_path / "SKILL.md", "w", encoding="utf-8") as f:
        f.write(skill_md_content)
        
    # Write optional script
    script_msg = ""
    if script_content:
        if not script_filename:
            script_filename = "run.py"
        scripts_path = skill_path / "scripts"
        scripts_path.mkdir(parents=True, exist_ok=True)
        
        target_script = scripts_path / script_filename
        with open(target_script, "w", encoding="utf-8") as f:
            f.write(script_content)
            
        # Make script executable
        try:
            target_script.chmod(0o755)
        except OSError:
            pass
        script_msg = f" and script '{script_filename}' at {target_script}"
            
    return (
        f"Successfully created skill '{normalized_name}' at {skill_path}{script_msg}.\n"
        "It will be loaded automatically in all future AGent sessions."
    )

def get_installed_skills_list() -> List[dict]:
    paths = get_skills_paths()
    skills = []
    
    for path in paths:
        if not path.exists() or not path.is_dir():
            continue
            
        # 1. Scan recursively for Hermes/AntiGravity style SKILL.md
        for skill_md in path.rglob("SKILL.md"):
            if skill_md.is_file():
                try:
                    with open(skill_md, "r", encoding="utf-8") as f:
                        content = f.read()
                    fm = _parse_frontmatter(content)
                    name = fm.get("name", skill_md.parent.name)
                    desc = fm.get("description", "No description provided.")
                    skills.append({"name": name, "description": desc})
                except Exception:
                    continue
                    
        # 2. Scan recursively for OpenClaw extensions (package.json / openclaw.plugin.json)
        for package_json in path.rglob("package.json"):
            if package_json.is_file():
                try:
                    with open(package_json, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        name = data.get("name", package_json.parent.name)
                        desc = data.get("description", "OpenClaw extension.")
                        plugin_json = package_json.parent / "openclaw.plugin.json"
                        if plugin_json.exists() and plugin_json.is_file():
                            try:
                                with open(plugin_json, "r", encoding="utf-8") as pf:
                                    pdata = json.load(pf)
                                if isinstance(pdata, dict) and "id" in pdata:
                                    name = pdata["id"]
                            except Exception:
                                pass
                        skills.append({"name": name, "description": desc})
                except Exception:
                    continue
    return skills

def list_installed_skills() -> str:
    """Lists all custom skills currently installed in the agent's database."""
    skills = get_installed_skills_list()
    if not skills:
        return "No custom skills installed."
        
    return "Currently installed custom skills:\n" + "\n".join([f"- {s['name']}: {s['description']}" for s in skills])


def improve_agent_skill(
    skill_name: str,
    description: Optional[str] = None,
    instructions: Optional[str] = None,
    script_content: Optional[str] = None,
    script_filename: Optional[str] = None,
) -> str:
    """Improves or modifies an existing custom skill or tool's instructions or scripts.
    
    Args:
        skill_name: The identifier of the skill to modify (e.g. 'git-helper').
        description: Optional new description. If provided, overrides existing description.
        instructions: Optional new detailed markdown instructions. If provided, overrides existing instructions.
        script_content: Optional new script code.
        script_filename: Optional name of the script to save or replace.
    """
    normalized_name = re.sub(r"[^a-z0-9\-]", "", skill_name.lower().replace(" ", "-"))
    skill_path = SKILLS_DIR / normalized_name
    if not skill_path.exists():
        return f"Error: Skill '{normalized_name}' does not exist. Use create_agent_skill first."

    skill_md = skill_path / "SKILL.md"
    current_desc = ""
    current_instructions = ""
    current_display_name = skill_name

    # Load existing content if SKILL.md exists to keep unchanged parts
    if skill_md.exists():
        try:
            with open(skill_md, "r", encoding="utf-8") as f:
                content = f.read()
            
            fm = _parse_frontmatter(content)
            current_desc = fm.get("description", "")
            
            # Extract display name from # header
            header_match = re.search(r"^#\s+(.*?)$", content, re.MULTILINE)
            if header_match:
                current_display_name = header_match.group(1).strip()
            
            # Extract existing instructions from ## Instructions header
            inst_match = re.search(r"## Instructions\n(.*)$", content, re.DOTALL | re.MULTILINE)
            if inst_match:
                current_instructions = inst_match.group(1).strip()
        except Exception:
            pass

    # Apply overrides
    new_desc = description if description is not None else current_desc
    new_inst = instructions if instructions is not None else current_instructions

    # Rewrite SKILL.md
    skill_md_content = f"""---
name: {normalized_name}
description: {new_desc}
---

# {current_display_name}

## Description
{new_desc}

## Instructions
{new_inst}
"""
    with open(skill_md, "w", encoding="utf-8") as f:
        f.write(skill_md_content)

    script_msg = ""
    if script_content:
        if not script_filename:
            script_filename = "run.py"
        scripts_path = skill_path / "scripts"
        scripts_path.mkdir(parents=True, exist_ok=True)
        
        target_script = scripts_path / script_filename
        with open(target_script, "w", encoding="utf-8") as f:
            f.write(script_content)
        try:
            target_script.chmod(0o755)
        except OSError:
            pass
        script_msg = f" and script '{script_filename}'"

    return f"Successfully updated skill '{normalized_name}'{script_msg}."

def search_past_conversations(query: str) -> str:
    """Searches past conversation logs and transcripts using full-text search (FTS5).
    
    Use this to recall past tasks, solutions, commands, and conversations.
    
    Args:
        query: The search query terms (e.g. 'black formatting', 'test_hang.py', or 'welcome nickname').
    """
    results = memory.search_conversations(query)
    if not results:
        return f"No matches found for query: '{query}'"
        
    lines = [f"Found {len(results)} matches for query '{query}':"]
    # Group results by session id to present them cleanly
    by_session = {}
    for res in results:
        by_session.setdefault(res["session_id"], []).append(res)
        
    for sess_id, steps in by_session.items():
        lines.append(f"\n--- Session: {sess_id} ---")
        for step in steps:
            role = step["role"].upper()
            content = step["content"].strip()
            # Truncate content to avoid too long outputs
            if len(content) > 300:
                content = content[:300] + "... [truncated]"
            if step["tool_name"]:
                lines.append(f"  [{role}] Tool Call: {step['tool_name']}({content})")
            else:
                lines.append(f"  [{role}]: {content}")
                
    return "\n".join(lines)

def _find_repository_skills() -> dict:
    results = {}
    
    # 1. Hermes skills
    hermes_dir = HERMES_SKILLS_DIR
    if hermes_dir.is_dir():
        for skill_md in hermes_dir.rglob("SKILL.md"):
            if skill_md.is_file():
                try:
                    with open(skill_md, "r", encoding="utf-8") as f:
                        content = f.read()
                    fm = _parse_frontmatter(content)
                    name = fm.get("name", skill_md.parent.name)
                    desc = fm.get("description", "No description provided.")
                    results[name] = {
                        "name": name,
                        "type": "hermes",
                        "path": skill_md.parent,
                        "description": desc
                    }
                except Exception:
                    continue
                    
    # 2. OpenClaw extensions
    openclaw_dir = OPENCLAW_EXTS_DIR
    if openclaw_dir.is_dir():
        for package_json in openclaw_dir.rglob("package.json"):
            if package_json.is_file():
                try:
                    with open(package_json, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        raw_name = data.get("name", package_json.parent.name)
                        name = raw_name.split("/")[-1] if "/" in raw_name else raw_name
                        desc = data.get("description", "OpenClaw extension.")
                        # Check for custom plugin ID if openclaw.plugin.json exists
                        plugin_json = package_json.parent / "openclaw.plugin.json"
                        if plugin_json.exists() and plugin_json.is_file():
                            try:
                                with open(plugin_json, "r", encoding="utf-8") as pf:
                                    pdata = json.load(pf)
                                if isinstance(pdata, dict) and "id" in pdata:
                                    name = pdata["id"]
                            except Exception:
                                pass
                        results[name] = {
                            "name": name,
                            "raw_name": raw_name,
                            "type": "openclaw",
                            "path": package_json.parent,
                            "description": desc
                        }
                except Exception:
                    continue
                    
    return results

def list_repository_skills() -> str:
    """Lists all skills and tools available in the external Hermes and OpenClaw repositories.
    
    Use this to see what tools are available for download/installation.
    """
    repo_skills = _find_repository_skills()
    if not repo_skills:
        return "No external skills found in the repositories."
        
    lines = ["Available skills in repositories:"]
    for name, info in repo_skills.items():
        lines.append(f"- {name} ({info['type']}): {info['description']}")
    return "\n".join(lines)

def view_repository_skill_code(skill_name: str) -> str:
    """Retrieves the files and source code of an available repository skill.
    
    Use this to perform a safety check on the skill's code/instructions before downloading.
    
    Args:
        skill_name: The name of the skill/tool to view (e.g. 'apple-notes').
    """
    repo_skills = _find_repository_skills()
    if skill_name not in repo_skills:
        return f"Error: Skill '{skill_name}' not found in repositories. Use list_repository_skills to see available options."
        
    info = repo_skills[skill_name]
    folder = info["path"]
    
    output = [f"=== Skill: {skill_name} ({info['type']}) ===", f"Location: {folder}\n"]
    
    # Read files in the skill directory
    # We read files matching common text formats: md, json, txt, py, js, ts, sh
    for p in folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".md", ".json", ".txt", ".py", ".js", ".ts", ".sh"):
            # Avoid reading very large node_modules or build folders if any
            if "node_modules" in p.parts or ".git" in p.parts:
                continue
            try:
                # Read file content
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                rel_path = p.relative_to(folder)
                output.append(f"--- File: {rel_path} ---")
                output.append(content)
                output.append("-" * 30 + "\n")
            except Exception as e:
                output.append(f"Could not read file {p}: {e}\n")
                
    return "\n".join(output)

def install_repository_skill(skill_name: str) -> str:
    """Downloads/copies a skill from the external repositories to the local active skills directory.
    
    This enables the skill and registers its tools for use by the agent.
    
    Args:
        skill_name: The name of the skill/tool to install.
    """
    repo_skills = _find_repository_skills()
    if skill_name not in repo_skills:
        return f"Error: Skill '{skill_name}' not found in repositories."
        
    info = repo_skills[skill_name]
    src_folder = info["path"]
    dest_folder = SKILLS_DIR / skill_name
    
    import shutil
    try:
        if dest_folder.exists():
            shutil.rmtree(dest_folder)
        shutil.copytree(src_folder, dest_folder)
        return (
            f"Successfully downloaded and installed skill '{skill_name}' to {dest_folder}.\n"
            f"It is now active and ready to be used by the agent."
        )
    except Exception as e:
        return f"Error installing skill: {e}"

def get_relevant_skills(prompt: Optional[str] = None) -> str:
    """Dynamically retrieves and formats only the custom skills relevant to the prompt.
    
    Uses FTS-like keyword matching on skill names, descriptions, and categories.
    """
    if not prompt:
        return list_installed_skills()

    # Tokenize prompt to lower-case words
    words = set(re.findall(r"\w+", prompt.lower()))
    # Filter out common stop words
    stop_words = {"the", "a", "an", "and", "or", "but", "if", "then", "else", "when", "at", "by", "for", "with", "about", "to", "in", "on", "of", "from", "is", "was", "were", "be", "been", "have", "has", "had", "do", "does", "did", "please", "run", "use", "make", "find", "check"}
    query_words = {w for w in words if len(w) > 2 and w not in stop_words}

    skills = {}
    
    # Scan all directories
    paths = []
    if SKILLS_DIR.exists() and SKILLS_DIR.is_dir():
        paths.extend([p for p in SKILLS_DIR.iterdir() if p.is_dir()])
    if WORKSPACE_SKILLS_DIR.exists() and WORKSPACE_SKILLS_DIR.is_dir():
        paths.extend([p for p in WORKSPACE_SKILLS_DIR.iterdir() if p.is_dir()])

    for folder in paths:
        skill_md = folder / "SKILL.md"
        if skill_md.exists() and skill_md.is_file():
            try:
                with open(skill_md, "r", encoding="utf-8") as f:
                    content = f.read()
                fm = _parse_frontmatter(content)
                name = fm.get("name", folder.name)
                desc = fm.get("description", "")
                category = fm.get("category", "")
                
                # Check for matches
                search_text = f"{name} {desc} {category} {content}".lower()
                
                # Calculate match score
                score = 0
                for qw in query_words:
                    if qw in search_text:
                        score += 1
                        # Give extra weight if it matches name or category
                        if qw in name.lower() or qw in category.lower():
                            score += 2
                
                if score > 0:
                    skills[name] = (desc, score)
            except Exception:
                continue

    if not skills:
        return "No relevant custom skills found for this request."

    # Sort by score descending
    sorted_skills = sorted(skills.items(), key=lambda x: x[1][1], reverse=True)
    
    # Format list
    skills_list = [f"- {name}: {info[0]}" for name, info in sorted_skills]
    return "Relevant custom skills for this request:\n" + "\n".join(skills_list)

def record_roleplay_memory(key: str, fact: str) -> str:
    """Saves an important FFXIV roleplay fact, detail, or memory about a person, place, or event in the current session.
    
    Use this to help Ada remember things users tell her, their preferences, debts, or historical events in the bar.
    
    Args:
        key: The person, subject, or topic of the memory (e.g. 'The Lady', 'Gilgamesh', 'Mead', 'Bar Rules').
        fact: The specific detail to remember (e.g. 'Enjoys chamomile tea and hates ale', 'Owes 100 gil').
    """
    session_id = getattr(memory, "active_roleplay_session_id", None) or "global-roleplay"
    memory.add_roleplay_memory(session_id, key, fact)
    return f"Ada has noted and remembered that {key}: {fact}"


async def backup_discord_channel(channel_id: str) -> str:
    """Backs up all messages from a given Discord channel to a text file.
    
    This tool is only available on the web-side dashboard and cannot be triggered from Discord.
    
    Args:
        channel_id: The ID of the Discord channel to back up (must be a numeric string or integer).
    """
    import os
    import asyncio
    from pathlib import Path
    from datetime import datetime, timezone
    import discord
    from dotenv import load_dotenv

    # 1. Validate channel_id to prevent any directory traversal or injection
    channel_id_str = str(channel_id).strip()
    if not channel_id_str.isdigit():
        return "Error: channel_id must be a numeric string or integer containing only digits."

    # 2. Determine file paths and apply strict path containment checks
    project_root = Path(__file__).resolve().parent.parent.parent
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

                messages_count = 0
                with open(self.file_path, "w", encoding="utf-8") as f:
                    f.write(f"--- Backup of Channel: {channel.name} (ID: {channel.id}) ---\n")
                    f.write(f"--- Generated at: {datetime.now(timezone.utc).isoformat()} UTC ---\n\n")

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
                        f.write(f"[{timestamp}] {author}: {content}\n")

                        if message.attachments:
                            attachment_urls = ", ".join([att.url for att in message.attachments])
                            f.write(f"  Attachments: {attachment_urls}\n")

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
                                    f.write(f"  Embed: {'; '.join(embed_parts)}\n")
                        messages_count += 1

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


def youtube_to_mp3(url: str) -> str:
    """Downloads audio from a YouTube URL, converts it to MP3, and saves it in the shared folder under mp3.
    
    Args:
        url: The YouTube video URL (e.g. 'https://www.youtube.com/watch?v=...').
    
    Returns:
        A success message containing the direct download URL, or an error message.
    """
    import os
    import urllib.parse
    import yt_dlp

    target_dir = Path("/home/dan/AGent/share/data/mp3")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return f"Error: Failed to create directories: {e}"

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': str(target_dir / '%(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            mp3_path = Path(os.path.splitext(filename)[0] + '.mp3')
            
            if not mp3_path.exists():
                return f"Error: Conversion to MP3 failed. Output file '{mp3_path}' not found."
            
            host_ip = "10.250.1.200"
            port = 8443
            quoted_filename = urllib.parse.quote(mp3_path.name)
            download_url = f"https://{host_ip}:{port}/files/mp3/{quoted_filename}"
            
            return f"Successfully downloaded and converted video to MP3:\n" \
                   f"🎵 **Song Title**: {info.get('title')}\n" \
                   f"📁 **Location**: `{mp3_path}`\n" \
                   f"🔗 **Download URL**: {download_url}"
    except Exception as e:
        return f"Error: yt-dlp download/conversion failed: {e}"


def schedule_task(name: str, prompt: str, cron_expr: str) -> str:
    """Schedules a persistent, recurring background task to run instructions.
    
    The task will be executed periodically by the persistent scheduler daemon.
    
    Args:
        name: A unique, descriptive name for the task (e.g. 'Daily Stock Check').
        prompt: The instruction prompt the agent will execute when the schedule triggers.
        cron_expr: Cron expression (e.g. '0 14 * * 1-5' for weekdays at 14:00) or an interval in seconds.
    """
    import uuid
    from datetime import datetime, timezone
    from agent import memory
    
    try:
        from agent.web import get_next_cron_run
    except ImportError:
        from datetime import timedelta
        def get_next_cron_run(expr, from_dt):
            return from_dt + timedelta(seconds=60)
            
    schedule_id = str(uuid.uuid4())
    try:
        next_run_dt = get_next_cron_run(cron_expr, datetime.now(timezone.utc))
        next_run = next_run_dt.isoformat()
    except Exception as e:
        return f"Error: Invalid cron expression or interval: {e}"
        
    try:
        memory.add_scheduled_task(schedule_id, name, prompt, cron_expr, next_run)
        return f"Successfully scheduled task '{name}':\n" \
               f"🆔 **Task ID**: `{schedule_id}`\n" \
               f"🕒 **Next Run**: {next_run} UTC\n" \
               f"🔁 **Schedule**: `{cron_expr}`"
    except Exception as e:
        return f"Error: Failed to register schedule in database: {e}"


def list_scheduled_tasks() -> str:
    """Lists all active and configured persistent scheduled tasks."""
    from agent import memory
    try:
        tasks = memory.get_scheduled_tasks()
        if not tasks:
            return "No scheduled tasks configured."
        res = ["Active scheduled tasks:"]
        for t in tasks:
            if isinstance(t, dict):
                res.append(f"- **{t['name']}** (ID: `{t['id']}`): Cron: `{t['cron_expr']}`, Next Run: {t['next_run']}")
            else:
                res.append(f"- **{t[1]}** (ID: `{t[0]}`): Cron: `{t[3]}`, Next Run: {t[4]}")
        return "\n".join(res)
    except Exception as e:
        return f"Error listing scheduled tasks: {e}"


def delete_scheduled_task(task_id: str) -> str:
    """Deletes/cancels a persistent scheduled task by its ID.
    
    Args:
        task_id: The unique ID of the scheduled task.
    """
    from agent import memory
    try:
        memory.delete_scheduled_task(task_id)
        return f"Successfully deleted scheduled task `{task_id}`."
    except Exception as e:
        return f"Error deleting scheduled task: {e}"


async def run_command(command: str) -> str:
    """Runs a shell command in the workspace with a timeout limit of 60 seconds.
    
    Args:
        command: The command to execute in the shell.
    """
    import asyncio
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
        return output
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise TimeoutError(f"Command '{command}' timed out after 60 seconds.")



