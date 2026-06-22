import re
import json
from pathlib import Path
from typing import List, Optional

from agent import memory

SKILLS_DIR = Path.home() / ".agent" / "skills"
OPENCLAW_EXTS_DIR = Path.home() / ".openclaw" / "extensions"
OPENCLAW_SKILLS_DIR = Path.home() / ".openclaw" / "skills"
HERMES_SKILLS_DIR = Path.home() / ".hermes" / "skills"

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



