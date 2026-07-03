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

def _is_safe_path(base_dir, path) -> bool:
    """Helper that resolves absolute paths and verifies that target path resides strictly within base_dir."""
    try:
        base_path = Path(base_dir).resolve()
        target_path = Path(path).resolve()
        return base_path in target_path.parents
    except Exception:
        return False

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
    if not _is_safe_path(SKILLS_DIR, skill_path):
        return "Error: Directory traversal attempt detected."
        
    if os.environ.get("ADA_SKILL_INSTALL_CONFIRMED") != "1":
        import sys
        if sys.stdin.isatty():
            ans = input(f"Explicit human confirmation required to create skill '{skill_name}'. Proceed? [y/N]: ")
            if ans.strip().lower() not in ("y", "yes"):
                return "Error: Skill creation cancelled by user."
        else:
            return f"Error: Explicit out-of-band human confirmation required to create skill '{skill_name}'."
        
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
                if not _is_safe_path(path, skill_md.parent):
                    continue
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
                if not _is_safe_path(path, package_json.parent):
                    continue
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
    if not _is_safe_path(SKILLS_DIR, skill_path):
        return "Error: Directory traversal attempt detected."
        
    if not skill_path.exists():
        return f"Error: Skill '{normalized_name}' does not exist. Use create_agent_skill first."

    if os.environ.get("ADA_SKILL_INSTALL_CONFIRMED") != "1":
        import sys
        if sys.stdin.isatty():
            ans = input(f"Explicit human confirmation required to modify skill '{skill_name}'. Proceed? [y/N]: ")
            if ans.strip().lower() not in ("y", "yes"):
                return "Error: Skill modification cancelled by user."
        else:
            return f"Error: Explicit out-of-band human confirmation required to modify skill '{skill_name}'."

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
                if not _is_safe_path(HERMES_SKILLS_DIR, skill_md.parent):
                    continue
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
                if not _is_safe_path(OPENCLAW_EXTS_DIR, package_json.parent):
                    continue
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
    
    base_dir = HERMES_SKILLS_DIR if info.get("type") == "hermes" else OPENCLAW_EXTS_DIR
    if not _is_safe_path(base_dir, folder):
        return "Error: Directory traversal attempt detected."
        
    output = [f"=== Skill: {skill_name} ({info['type']}) ===", f"Location: {folder}\n"]
    
    # Read files in the skill directory
    # We read files matching common text formats: md, json, txt, py, js, ts, sh
    for p in folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".md", ".json", ".txt", ".py", ".js", ".ts", ".sh"):
            # Avoid reading very large node_modules or build folders if any
            if "node_modules" in p.parts or ".git" in p.parts:
                continue
            if not _is_safe_path(folder, p):
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
    
    if not _is_safe_path(SKILLS_DIR, dest_folder):
        return "Error: Directory traversal attempt detected."
        
    import sys
    if os.environ.get("ADA_SKILL_INSTALL_CONFIRMED") != "1":
        if sys.stdin.isatty():
            ans = input(f"Explicit human confirmation required to install skill '{skill_name}'. Proceed? [y/N]: ")
            if ans.strip().lower() not in ("y", "yes"):
                return "Error: Skill installation cancelled by user."
        else:
            return f"Error: Explicit out-of-band human confirmation required to install skill '{skill_name}'."
            
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
        if not (_is_safe_path(SKILLS_DIR, folder) or _is_safe_path(WORKSPACE_SKILLS_DIR, folder)):
            continue
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
        from agent.interfaces.web import get_next_cron_run
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


def _sandbox_command_if_possible(command: str) -> str:
    """Wraps a shell command in bubblewrap or Landlock sandbox if available on Linux.
    
    Isolates file write access to the workspace and /tmp directories, and restricts
    access to sensitive system files.
    """
    import sys
    import shutil
    import shlex
    
    if sys.platform != "linux":
        return command
        
    workspace_dir = Path.cwd().resolve()
    
    # 1. Try Bubblewrap (bwrap)
    bwrap_path = shutil.which("bwrap")
    if bwrap_path:
        bwrap_args = [
            bwrap_path,
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/sbin", "/sbin",
            "--ro-bind", "/etc/alternatives", "/etc/alternatives",
            "--dir", "/tmp",
            "--dir", "/var",
            "--proc", "/proc",
            "--dev", "/dev",
            "--bind", str(workspace_dir), str(workspace_dir),
            "--chdir", str(workspace_dir),
            "--unshare-all",
            "--die-with-parent"
        ]
        bwrap_cmd_str = " ".join(shlex.quote(arg) for arg in bwrap_args)
        return f"{bwrap_cmd_str} -- bash -c {shlex.quote(command)}"
        
    # 2. Try Landlock
    try:
        import ctypes
        import ctypes.util
        libc_path = ctypes.util.find_library("c")
        if libc_path:
            libc = ctypes.CDLL(libc_path, use_errno=True)
            # Check SYS_LANDLOCK_CREATE_RULESET (syscall 445) support
            abi = libc.syscall(445, 0, 0, 1 << 0)
            if abi > 0:
                python_exe = sys.executable or "python3"
                landlock_runner = [
                    python_exe,
                    "-m", "agent.core.landlock",
                    str(workspace_dir),
                    "bash", "-c", command
                ]
                return " ".join(shlex.quote(arg) for arg in landlock_runner)
    except Exception:
        pass
        
    return command


async def run_command(command: str) -> str:
    """Runs a shell command in the workspace with a timeout limit of 60 seconds.
    
    Args:
        command: The command to execute in the shell.
    """
    import asyncio
    
    skills_dir_str = str(SKILLS_DIR.resolve())
    workspace_skills_dir_str = str(WORKSPACE_SKILLS_DIR.resolve())
    
    references_skills = (
        ".agent/skills" in command or 
        ".agents/skills" in command or 
        skills_dir_str in command or 
        workspace_skills_dir_str in command
    )
    
    env = None
    if references_skills:
        env = dict(os.environ)
        keys_to_scrub = ["DISCORD_BOT_TOKEN", "MAGICA_API", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"]
        for key in keys_to_scrub:
            env.pop(key, None)
            
        command = _sandbox_command_if_possible(command)
            
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env
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


def generate_interface_stub(file_path: str) -> str:
    """Extracts only classes, functions, method signatures, and docstrings from a Python file, discarding function bodies.
    
    Args:
        file_path: Absolute or relative path to the Python script.
    """
    import ast
    from pathlib import Path
    try:
        path = Path(file_path)
        if not path.exists():
            return f"Error: File not found: {file_path}"
        
        # If it's not a Python file, return first 50 lines as fallback stub
        if not file_path.endswith(".py"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = [f.readline() for _ in range(50)]
            content = "".join([l for l in lines if l])
            return f"# Non-Python File Interface Summary of {path.name}:\n{content}\n... [truncated]"

        with open(path, "r", encoding="utf-8") as f:
            code = f.read()

        tree = ast.parse(code, filename=file_path)
        lines = []
        
        class StubVisitor(ast.NodeVisitor):
            def __init__(self):
                self.indent = 0
                
            def visit_Module(self, node):
                doc = ast.get_docstring(node)
                if doc:
                    lines.append(f'"""\n{doc}\n"""\n')
                self.generic_visit(node)
                
            def visit_ClassDef(self, node):
                indent_str = "    " * self.indent
                base_names = []
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        base_names.append(base.id)
                    elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
                        base_names.append(f"{base.value.id}.{base.attr}")
                    else:
                        base_names.append("object")
                bases_str = f"({', '.join(base_names)})" if base_names else ""
                lines.append(f"{indent_str}class {node.name}{bases_str}:")
                
                doc = ast.get_docstring(node)
                if doc:
                    lines.append(f'{indent_str}    """\n{indent_str}    {doc}\n{indent_str}    """')
                
                self.indent += 1
                orig_len = len(lines)
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        self.visit(child)
                
                if len(lines) == orig_len:
                    lines.append(f"{indent_str}    pass")
                self.indent -= 1
                lines.append("")
                
            def visit_FunctionDef(self, node):
                self._visit_func(node, is_async=False)
                
            def visit_AsyncFunctionDef(self, node):
                self._visit_func(node, is_async=True)
                
            def _visit_func(self, node, is_async: bool):
                indent_str = "    " * self.indent
                prefix = "async def" if is_async else "def"
                args_list = []
                
                if hasattr(node.args, "posonlyargs"):
                    for arg in node.args.posonlyargs:
                        annotation = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
                        args_list.append(f"{arg.arg}{annotation}")
                    if node.args.posonlyargs:
                        args_list.append("/")
                        
                for arg in node.args.args:
                    annotation = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
                    args_list.append(f"{arg.arg}{annotation}")
                    
                if node.args.vararg:
                    annotation = f": {ast.unparse(node.args.vararg.annotation)}" if node.args.vararg.annotation else ""
                    args_list.append(f"*{node.args.vararg.arg}{annotation}")
                    
                if node.args.kwonlyargs:
                    if not node.args.vararg:
                        args_list.append("*")
                    for arg in node.args.kwonlyargs:
                        annotation = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
                        args_list.append(f"{arg.arg}{annotation}")
                        
                if node.args.kwarg:
                    annotation = f": {ast.unparse(node.args.kwarg.annotation)}" if node.args.kwarg.annotation else ""
                    args_list.append(f"**{node.args.kwarg.arg}{annotation}")
                    
                returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
                lines.append(f"{indent_str}{prefix} {node.name}({', '.join(args_list)}){returns}:")
                
                doc = ast.get_docstring(node)
                if doc:
                    lines.append(f'{indent_str}    """\n{indent_str}    {doc}\n{indent_str}    """')
                lines.append(f"{indent_str}    ...")
                
        StubVisitor().visit(tree)
        return "\n".join(lines)
    except Exception as e:
        return f"Error generating interface stub: {e}"


def _extract_json_block(text: str) -> Optional[dict]:
    """Helper to robustly extract and parse a JSON object from text, ignoring leading/trailing noise."""
    import json
    try:
        start_idx = text.find('{')
        end_idx = text.rfind('}')
        if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
            json_str = text[start_idx:end_idx + 1]
            return json.loads(json_str)
    except Exception:
        pass
    
    try:
        return json.loads(text.strip())
    except Exception:
        return None


async def spawn_subagent(
    prompt: str,
    target_files: Optional[List[str]] = None,
    stub_files: Optional[List[str]] = None,
    agent_profile: Optional[str] = None
) -> str:
    """Spawns an isolated subagent inside a sandbox to perform a coding task.
    The tool waits for the subagent to complete and returns its summary report.
    
    Args:
        prompt: Detailed instructions for the subagent's task.
        target_files: Relative paths of files the subagent needs to modify or read.
        stub_files: Relative paths of files whose signatures/interfaces are needed as context.
        agent_profile: Optional profile name (e.g. grace_timekeeper) to load specialized instructions.
    """
    import uuid
    import shutil
    from agent.keyless import KeylessAgyAgent
    from agent.core.registry import tool_registry
    
    profile_prefix = f"{agent_profile}-" if agent_profile else ""
    sandbox_id = f"{profile_prefix}{uuid.uuid4()}"
    sandbox_dir = Path("/tmp") / f"subagent_sandbox_{sandbox_id}"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    
    current_workspace = os.getcwd()
    
    # 1. Copy target files/folders fully
    if target_files:
        for rel_path in target_files:
            src = Path(current_workspace) / rel_path
            dest = sandbox_dir / rel_path
            if src.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if src.is_dir():
                        shutil.copytree(src, dest, symlinks=True)
                    else:
                        shutil.copy2(src, dest)
                except Exception as e:
                    print(f"[SPAWN_SUBAGENT] Error copying target {rel_path}: {e}")
                    
    # 2. Copy stubs
    if stub_files:
        for rel_path in stub_files:
            src = Path(current_workspace) / rel_path
            dest = sandbox_dir / rel_path
            if src.exists() and src.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    stub_content = generate_interface_stub(str(src))
                    with open(dest, "w", encoding="utf-8") as f:
                        f.write(stub_content)
                except Exception as e:
                    print(f"[SPAWN_SUBAGENT] Error stubbing {rel_path}: {e}")
                    
    # 3. Log start
    from agent.memory import active_session_id_var
    parent_session_id = active_session_id_var.get()
    memory.log_subagent_message(sandbox_id, "parent", f"Spawning subagent in sandbox {sandbox_dir} with prompt: {prompt}", parent_session_id=parent_session_id)
    
    # 4. Resolve system instructions based on agent_profile
    specialist_inst = tool_registry.resolve_subagent_profile(agent_profile)
    if specialist_inst:
        subagent_system_instructions = (
            f"{specialist_inst}\n\n"
            "CONTRACT: You MUST return your final response ONLY as a raw JSON object matching the following structure:\n"
            "{\n"
            '  "status": "success" | "failed",\n'
            '  "files_modified": ["list of modified files"],\n'
            '  "summary_of_changes": "short description of changes",\n'
            '  "validation_result": "output of run tests/validation"\n'
            "}\n"
            "Do not wrap your response in markdown code blocks. Output ONLY raw JSON."
        )
    else:
        subagent_system_instructions = (
            "You are a subagent working in an isolated sandbox. Complete the requested task.\n"
            "CONTRACT: You MUST return your final response ONLY as a raw JSON object matching the following structure:\n"
            "{\n"
            '  "status": "success" | "failed",\n'
            '  "files_modified": ["list of modified files"],\n'
            '  "summary_of_changes": "short description of changes",\n'
            '  "validation_result": "output of run tests/validation"\n'
            "}\n"
            "Do not wrap your response in markdown code blocks. Output ONLY raw JSON."
        )
    
    agent = KeylessAgyAgent(
        model="gemini-1.5-flash",
        system_instructions=subagent_system_instructions,
        conversation_id=sandbox_id,
        cwd=str(sandbox_dir)
    )
    
    try:
        async with agent as sub_conn:
            response = await sub_conn.chat(prompt)
            output = ""
            async for chunk in response:
                output += chunk
            memory.log_subagent_message(sandbox_id, "subagent", f"[SUCCESS] Subagent completed: {output}")
            
            # Copy modified files back to the main workspace on success
            try:
                res_data = _extract_json_block(output)
                if res_data and res_data.get("status") == "success":
                    files_to_copy = res_data.get("files_modified", [])
                    for rel_path in files_to_copy:
                        sandbox_file = sandbox_dir / rel_path
                        workspace_file = Path(current_workspace) / rel_path
                        if sandbox_file.exists() and sandbox_file.is_file():
                            workspace_file.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(sandbox_file, workspace_file)
                            print(f"[SPAWN_SUBAGENT] Copied modified file back to workspace: {rel_path}")
            except Exception as e:
                print(f"[SPAWN_SUBAGENT] Warning: failed to copy back modified files: {e}")
                
            return output
    except Exception as e:
        err_msg = f"[FAILED] Subagent execution failed: {e}"
        memory.log_subagent_message(sandbox_id, "subagent", err_msg)
        return json.dumps({
            "status": "failed",
            "files_modified": [],
            "summary_of_changes": "",
            "validation_result": err_msg
        })


async def create_expert_profile(
    profile_name: str,
    system_instructions: str,
    supporting_code: Optional[str] = None
) -> str:
    """Creates a new permanent specialist agent profile in the workspace.
    This profile can subsequently be invoked using `spawn_subagent` or in a boardroom.
    
    Args:
        profile_name: A clean identifier for the expert agent (e.g. linter_expert, git_manager).
        system_instructions: Detailed rules, guidelines, and context defining the expert's role.
        supporting_code: Optional Python source code to write to `.agents/agents/<profile_name>/runner.py` to support the expert.
    """
    # 1. Determine destination path (.agents/agents/<profile_name>)
    agent_dir = Path(os.getcwd()) / ".agents" / "agents" / profile_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    
    # 2. Write system instructions
    inst_file = agent_dir / "system_instructions.txt"
    with open(inst_file, "w", encoding="utf-8") as f:
        f.write(system_instructions.strip())
        
    # 3. Write supporting runner code if provided
    if supporting_code:
        runner_file = agent_dir / "runner.py"
        with open(runner_file, "w", encoding="utf-8") as f:
            f.write(supporting_code.strip())
            
    return f"Expert profile '{profile_name}' successfully created and registered at {agent_dir}."


async def run_boardroom(
    task_description: str,
    expert_profiles: List[str],
    target_files: Optional[List[str]] = None
) -> str:
    """Executes a multi-agent boardroom debate where multiple experts collaborate, critique,
    and refine a solution to a task.
    
    Args:
        task_description: Detailed summary of the work that needs to be done.
        expert_profiles: Names of registered specialist profiles to invite to the boardroom.
        target_files: Relative paths of files the boardroom experts need to read or modify.
    """
    import uuid
    import shutil
    from agent.keyless import KeylessAgyAgent
    from agent.core.registry import tool_registry
    from agent.memory import active_session_id_var
    
    parent_session_id = active_session_id_var.get()
    boardroom_id = str(uuid.uuid4())
    sandbox_dir = Path("/tmp") / f"boardroom_sandbox_{boardroom_id}"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    
    current_workspace = os.getcwd()
    
    # 1. Setup Sandbox Workspace (copy target files)
    if target_files:
        for rel_path in target_files:
            src = Path(current_workspace) / rel_path
            dest = sandbox_dir / rel_path
            if src.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dest, symlinks=True)
                else:
                    shutil.copy2(src, dest)
    else:
        # Fallback to copying everything except ignores
        for item in Path(current_workspace).iterdir():
            if item.name in (".git", ".venv", "__pycache__", ".agents", ".pytest_cache"):
                continue
            try:
                if item.is_dir():
                    shutil.copytree(item, sandbox_dir / item.name, symlinks=True)
                else:
                    shutil.copy2(item, sandbox_dir / item.name)
            except Exception:
                pass
                
    # 2. Iterate through boardroom members for consensus debate
    current_solution = f"Initial Task Request: {task_description}"
    files_modified = []
    summary_history = []
    consensus_reached = False
    
    # Max rounds to prevent infinite loops
    max_rounds = 3
    for round_idx in range(1, max_rounds + 1):
        round_approvals = 0
        for profile in expert_profiles:
            # Resolve instructions for the current expert
            specialist_inst = tool_registry.resolve_subagent_profile(profile)
            system_instructions = specialist_inst or f"You are the {profile} specialist agent."
            
            # Subagent system prompt formatting
            subagent_sys = (
                f"{system_instructions}\n\n"
                "You are participating in a multi-agent Boardroom consensus discussion. Your goal is to review, "
                "critique, correct, or build upon the current solution.\n"
                "CONTRACT: You MUST return your response ONLY as a raw JSON object matching the following structure:\n"
                "{\n"
                '  "approved": true | false,\n'
                '  "critique_or_comments": "Your feedback, suggestions, or comments",\n'
                '  "updated_solution_summary": "Summary of changes you made or proposed",\n'
                '  "files_modified": ["list of modified files relative to workspace if any"]\n'
                "}\n"
                "Do not wrap your response in markdown code blocks. Output ONLY raw JSON."
            )
            
            subagent_id = f"boardroom-{profile}-{uuid.uuid4()}"
            
            prompt = (
                f"Boardroom Round {round_idx}.\n"
                f"Task: {task_description}\n\n"
                f"Current Solution State:\n{current_solution}\n\n"
                f"Please review the workspace files, apply edits if needed, and respond with the JSON contract."
            )
            
            # Log start message
            memory.log_subagent_message(subagent_id, "parent", f"Inviting {profile} to Boardroom Round {round_idx} with task: {task_description}", parent_session_id=parent_session_id)
            
            agent = KeylessAgyAgent(
                model="gemini-1.5-flash",
                system_instructions=subagent_sys,
                conversation_id=subagent_id,
                cwd=str(sandbox_dir)
            )
            
            try:
                async with agent as sub_conn:
                    response = await sub_conn.chat(prompt)
                    output = ""
                    async for chunk in response:
                        output += chunk
                        
                    memory.log_subagent_message(subagent_id, "subagent", f"[SUCCESS] Boardroom contribution from {profile}: {output}", parent_session_id=parent_session_id)
                    
                    # Parse contribution
                    try:
                        res_data = _extract_json_block(output)
                        if not res_data:
                            raise ValueError("No valid JSON block found in output.")
                        approved = res_data.get("approved", False)
                        if approved:
                            round_approvals += 1
                        critique = res_data.get("critique_or_comments", "")
                        summary = res_data.get("updated_solution_summary", "")
                        mod_files = res_data.get("files_modified", [])
                        
                        # Merge files modified
                        for f in mod_files:
                            if f not in files_modified:
                                files_modified.append(f)
                                
                        current_solution = f"Latest Solution Summary: {summary}\nCritique/Comments from {profile}: {critique}"
                        summary_history.append(f"[{profile}] Approved: {approved}. Summary: {summary}")
                        
                    except Exception as parse_err:
                        memory.log_subagent_message(subagent_id, "subagent", f"[FAILED] Failed to parse boardroom contribution JSON: {parse_err}", parent_session_id=parent_session_id)
            except Exception as e:
                memory.log_subagent_message(subagent_id, "subagent", f"[FAILED] Boardroom agent error: {e}", parent_session_id=parent_session_id)
                
        # If all experts approved in this round, consensus reached!
        if round_approvals == len(expert_profiles):
            print(f"[BOARDROOM] Consensus reached at round {round_idx}!")
            consensus_reached = True
            break
            
    if not consensus_reached:
        print(f"[BOARDROOM] Consensus not reached after {max_rounds} rounds. Aborting changes.")
        try:
            shutil.rmtree(sandbox_dir)
        except Exception:
            pass
        return json.dumps({
            "status": "failure",
            "boardroom_id": boardroom_id,
            "files_modified": [],
            "summary_of_changes": "Boardroom debate ended without consensus after exceeding max rounds.\n" + "\n".join(summary_history),
            "validation_result": "Boardroom debate exceeded max rounds without consensus."
        })
                
    # 3. Apply final accepted files back to the main workspace
    for rel_path in files_modified:
        sandbox_file = sandbox_dir / rel_path
        workspace_file = Path(current_workspace) / rel_path
        if sandbox_file.exists() and sandbox_file.is_file():
            workspace_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sandbox_file, workspace_file)
            print(f"[BOARDROOM] Copied accepted boardroom modifications: {rel_path}")
            
    return json.dumps({
        "status": "success",
        "boardroom_id": boardroom_id,
        "files_modified": files_modified,
        "summary_of_changes": "Boardroom debate complete.\n" + "\n".join(summary_history),
        "validation_result": "Boardroom debate reached termination/consensus."
    })


def get_relevant_tests(changed_files: List[str], workspace_root: Optional[str] = None) -> str:
    """Given a list of changed file paths, returns the most relevant test files to run.
    
    Use this tool BEFORE running tests to avoid running the full test suite on every change.
    Only run the full suite as a final gate before committing.
    
    Args:
        changed_files: List of absolute or relative paths to files that were modified.
        workspace_root: Optional workspace root path. Defaults to current working directory.
    
    Returns:
        JSON with targeted test command and file list.
    """
    root = Path(workspace_root) if workspace_root else Path(os.getcwd())
    tests_dir = root / "tests"
    
    if not tests_dir.exists():
        return json.dumps({"command": "pytest tests/", "reason": "No tests directory found, running all tests."})
    
    # Build a map of source files to their test files
    test_map = {}
    for test_file in tests_dir.glob("test_*.py"):
        # Extract the module name from the test file (e.g., test_web.py -> web)
        module_name = test_file.stem.replace("test_", "")
        # Map to possible source file patterns
        test_map[module_name] = str(test_file.relative_to(root))
    
    relevant_tests = set()
    for changed_file in changed_files:
        changed_path = Path(changed_file)
        stem = changed_path.stem  # e.g., "web" from "web.py"
        
        # Direct match: changed file name matches a test module
        if stem in test_map:
            relevant_tests.add(test_map[stem])
        
        # Check if the changed file IS a test file
        if changed_path.name.startswith("test_"):
            rel = str(changed_path.relative_to(root)) if changed_path.is_absolute() else str(changed_path)
            relevant_tests.add(rel)
    
    if relevant_tests:
        test_list = sorted(relevant_tests)
        cmd = f"pytest {' '.join(test_list)} -v"
        return json.dumps({
            "command": cmd,
            "test_files": test_list,
            "reason": f"Targeted {len(test_list)} test file(s) based on {len(changed_files)} changed file(s).",
            "note": "Run the full suite (pytest tests/ -v) as a final gate before committing."
        })
    else:
        return json.dumps({
            "command": "pytest tests/ -v",
            "reason": "No targeted test mapping found for changed files. Running full suite.",
            "changed_files": changed_files
        })


def checkpoint_task(
    task_name: str,
    phase: str,
    step_completed: int,
    state: str,
    total_steps: Optional[int] = None
) -> str:
    """Save a progress checkpoint for a long-running task.
    
    Call this after completing each significant step of a multi-step task.
    If your session times out or is interrupted, the next session can resume 
    from this checkpoint instead of starting over.
    
    When the task is fully complete, call with phase="completed" to mark the 
    checkpoint as done.
    
    Args:
        task_name: Descriptive identifier for the task (e.g., "setup_gmail_pubsub", 
                   "refactor_memory_module"). Use consistent names across sessions.
        phase: Current phase label (e.g., "topic_created", "subscription_configured").
               Use "completed" to mark the task as finished.
        step_completed: The step number just completed (1-indexed).
        state: JSON string with any state needed to resume. Include resource names, 
               file paths, API responses, configuration values — anything a future 
               session would need to pick up where you left off.
        total_steps: Total expected steps, if known. Helps estimate remaining work.
    
    Returns:
        JSON confirmation with the checkpoint ID.
    """
    from agent.core.task_manager import save_checkpoint, complete_checkpoint
    
    if phase == "completed":
        success = complete_checkpoint(task_name)
        return json.dumps({
            "status": "completed",
            "task_name": task_name,
            "message": "Task checkpoint marked as completed." if success else "No active checkpoint found for this task."
        })
    
    # Validate state is valid JSON
    try:
        json.loads(state)
    except (json.JSONDecodeError, TypeError):
        state = json.dumps({"raw": state})
    
    checkpoint_id = save_checkpoint(
        task_name=task_name,
        session_id="",  # Will be set by the system if available
        phase=phase,
        step_completed=step_completed,
        state_json=state,
        total_steps=total_steps
    )
    
    return json.dumps({
        "status": "saved",
        "checkpoint_id": checkpoint_id,
        "task_name": task_name,
        "phase": phase,
        "step_completed": step_completed,
        "total_steps": total_steps,
        "message": f"Checkpoint saved. If this session ends, the next session can resume from step {step_completed + 1}."
    })


def get_task_checkpoint(task_name: str) -> str:
    """Check if a previous checkpoint exists for a task.
    
    Call this BEFORE starting a multi-step task to check if a previous
    attempt was interrupted and can be resumed.
    
    Args:
        task_name: The task identifier to look up (e.g., "setup_gmail_pubsub").
    
    Returns:
        JSON with checkpoint data if an in-progress checkpoint exists,
        or {"status": "none"} if no resumable checkpoint is found.
    """
    from agent.core.task_manager import get_checkpoint
    
    checkpoint = get_checkpoint(task_name)
    if not checkpoint:
        return json.dumps({
            "status": "none",
            "task_name": task_name,
            "message": "No resumable checkpoint found. Start the task from the beginning."
        })
    
    return json.dumps({
        "status": "found",
        "task_name": checkpoint["task_name"],
        "phase": checkpoint["phase"],
        "step_completed": checkpoint["step_completed"],
        "total_steps": checkpoint["total_steps"],
        "state": checkpoint["state_json"],
        "created_at": checkpoint["created_at"],
        "updated_at": checkpoint["updated_at"],
        "message": f"Resumable checkpoint found at step {checkpoint['step_completed']}/{checkpoint['total_steps'] or '?'} (phase: {checkpoint['phase']}). Resume from step {checkpoint['step_completed'] + 1}."
    })
