import os
import re
import json
import time
import shutil
import tempfile
import urllib.request
from pathlib import Path
from typing import List, Optional

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
            result[k.strip()] = v.strip()
    return result

def get_skills_paths() -> List[Path]:
    """Returns a list of directories containing custom skills or tools."""
    from agent.execution import tools
    paths = [tools.SKILLS_DIR]
    if tools.OPENCLAW_EXTS_DIR.exists() and tools.OPENCLAW_EXTS_DIR.is_dir():
        paths.append(tools.OPENCLAW_EXTS_DIR)
    if tools.OPENCLAW_SKILLS_DIR.exists() and tools.OPENCLAW_SKILLS_DIR.is_dir():
        paths.append(tools.OPENCLAW_SKILLS_DIR)
    if tools.HERMES_SKILLS_DIR.exists() and tools.HERMES_SKILLS_DIR.is_dir():
        paths.append(tools.HERMES_SKILLS_DIR)
    return paths

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
    from agent.execution import tools
    # Normalize name to lowercase, alphanumeric and hyphens
    normalized_name = re.sub(r"[^a-z0-9\-]", "", skill_name.lower().replace(" ", "-"))
    if not normalized_name:
        return "Error: skill_name must contain alphanumeric characters or hyphens."
        
    skill_path = tools.SKILLS_DIR / normalized_name
    if not tools._is_safe_path(tools.SKILLS_DIR, skill_path):
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
        
        target_script = (scripts_path / script_filename).resolve()
        if not tools._is_safe_path(tools.SKILLS_DIR, target_script):
            return "Error: Directory traversal attempt in script_filename detected."
            
        scripts_path.mkdir(parents=True, exist_ok=True)
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
    from agent.execution import tools
    paths = get_skills_paths()
    skills = []
    
    for path in paths:
        if not path.exists() or not path.is_dir():
            continue
            
        # 1. Scan recursively for Hermes/AntiGravity style SKILL.md
        for skill_md in path.rglob("SKILL.md"):
            if skill_md.is_file():
                if not tools._is_safe_path(path, skill_md.parent):
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
                if not tools._is_safe_path(path, package_json.parent):
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
    from agent.execution import tools
    normalized_name = re.sub(r"[^a-z0-9\-]", "", skill_name.lower().replace(" ", "-"))
    skill_path = tools.SKILLS_DIR / normalized_name
    if not tools._is_safe_path(tools.SKILLS_DIR, skill_path):
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

def _find_repository_skills() -> dict:
    from agent.execution import tools
    results = {}
    
    # 1. Hermes local installed skills
    hermes_dir = tools.HERMES_SKILLS_DIR
    if hermes_dir.is_dir():
        for skill_md in hermes_dir.rglob("SKILL.md"):
            if skill_md.is_file():
                if not tools._is_safe_path(tools.HERMES_SKILLS_DIR, skill_md.parent):
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
                        "path": str(skill_md.parent),
                        "description": desc,
                        "remote": False,
                        "source": "hermes-local"
                    }
                except Exception:
                    continue

    # 2. Hermes local optional skills (cloned repo)
    optional_dir = Path.home() / ".hermes" / "hermes-agent" / "optional-skills"
    if optional_dir.is_dir():
        for skill_md in optional_dir.rglob("SKILL.md"):
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
                        "path": str(skill_md.parent),
                        "description": desc,
                        "remote": False,
                        "source": "hermes-optional"
                    }
                except Exception:
                    continue

    # 3. OpenClaw local extensions
    openclaw_dir = tools.OPENCLAW_EXTS_DIR
    if openclaw_dir.is_dir():
        for package_json in openclaw_dir.rglob("package.json"):
            if package_json.is_file():
                if not tools._is_safe_path(tools.OPENCLAW_EXTS_DIR, package_json.parent):
                    continue
                try:
                    with open(package_json, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        raw_name = data.get("name", package_json.parent.name)
                        name = raw_name.split("/")[-1] if "/" in raw_name else raw_name
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
                        results[name] = {
                            "name": name,
                            "raw_name": raw_name,
                            "type": "openclaw",
                            "path": str(package_json.parent),
                            "description": desc,
                            "remote": False,
                            "source": "openclaw-local"
                        }
                except Exception:
                    continue

    # 4. Fetch and merge remote web stores (ClawHub and Hermes Index)
    cache_dir = Path.home() / ".agent" / "cache"
    cache_file = cache_dir / "remote_repository_skills.json"
    cache_ttl = 3600  # 1 hour
    
    loaded_from_cache = False
    remote_results = {}
    
    if cache_file.is_file():
        try:
            age = time.time() - cache_file.stat().st_mtime
            if age < cache_ttl:
                with open(cache_file, "r", encoding="utf-8") as f:
                    remote_results = json.load(f)
                loaded_from_cache = True
        except Exception:
            pass
            
    if not loaded_from_cache:
        # Fetch ClawHub
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://clawhub.ai/api/v1/skills?limit=250",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                claw_data = json.loads(response.read().decode())
                for item in claw_data.get("items", []):
                    slug = item.get("slug")
                    if slug:
                        name = item.get("displayName") or slug
                        desc = item.get("summary") or item.get("description") or "OpenClaw extension."
                        remote_results[slug] = {
                            "name": name,
                            "type": "openclaw",
                            "description": desc,
                            "identifier": slug,
                            "remote": True,
                            "source": "clawhub"
                        }
        except Exception:
            pass
            
        # Fetch Hermes Index
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://hermes-agent.nousresearch.com/docs/api/skills-index.json",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                hermes_data = json.loads(response.read().decode())
                for skill in hermes_data.get("skills", []):
                    if skill.get("source") == "official" or skill.get("trust_level") == "builtin":
                        name = skill.get("name")
                        if name:
                            desc = skill.get("description") or "Hermes skill."
                            identifier = skill.get("identifier")
                            repo = skill.get("repo")
                            path_val = skill.get("path")
                            remote_results[name] = {
                                "name": name,
                                "type": "hermes",
                                "description": desc,
                                "identifier": identifier,
                                "remote": True,
                                "source": "hermes-index",
                                "repo": repo,
                                "path": path_val
                            }
        except Exception:
            pass
            
        if remote_results:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(remote_results, f)
            except Exception:
                pass
            
    for k, v in remote_results.items():
        if k not in results:
            results[k] = v
            
    return results

def list_repository_skills() -> str:
    """Lists all skills and tools available in the external Hermes and OpenClaw repositories.
    
    Use this to see what tools are available for download/installation.
    """
    from agent.execution import tools
    repo_skills = tools._find_repository_skills()
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
    from agent.execution import tools
    repo_skills = tools._find_repository_skills()
    if skill_name not in repo_skills:
        return f"Error: Skill '{skill_name}' not found in repositories. Use list_repository_skills to see available options."
        
    info = repo_skills[skill_name]
    output = [f"=== Skill: {skill_name} ({info['type']}) ==="]
    
    if not info.get("remote") and "path" in info and info["path"]:
        folder = Path(info["path"])
        
        allowed_bases = [
            tools.HERMES_SKILLS_DIR,
            tools.OPENCLAW_EXTS_DIR,
            Path.home() / ".hermes" / "hermes-agent" / "optional-skills"
        ]
        is_safe = False
        for base in allowed_bases:
            if base.exists():
                try:
                    if tools._is_safe_path(base, folder) or folder.resolve() == base.resolve():
                        is_safe = True
                        break
                except Exception:
                    pass
        
        if not is_safe:
            opt_base = (Path.home() / ".hermes" / "hermes-agent" / "optional-skills").resolve()
            try:
                if folder.resolve().is_relative_to(opt_base):
                    is_safe = True
            except Exception:
                pass
                
        if not is_safe:
            return "Error: Directory traversal attempt detected."
            
        output.append(f"Location: {folder}\n")
        
        for p in folder.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".md", ".json", ".txt", ".py", ".js", ".ts", ".sh"):
                if "node_modules" in p.parts or ".git" in p.parts:
                    continue
                if not tools._is_safe_path(folder, p):
                    continue
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    rel_path = p.relative_to(folder)
                    output.append(f"--- File: {rel_path} ---")
                    output.append(content)
                    output.append("-" * 30 + "\n")
                except Exception as e:
                    output.append(f"Could not read file {p}: {e}\n")
    else:
        output.append("Location: Remote Web Store\n")
        if info["type"] == "openclaw":
            try:
                slug = info["identifier"]
                # get latest version
                req = urllib.request.Request(f"https://clawhub.ai/api/v1/skills/{slug}", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read().decode())
                    skill_payload = data.get("skill", data)
                    latest_version = data.get("latestVersion", {}).get("version") or skill_payload.get("latestVersion", {}).get("version")
                    
                if not latest_version:
                    return f"Error: Could not resolve latest version for OpenClaw skill '{skill_name}'"
                    
                # get files
                req_files = urllib.request.Request(f"https://clawhub.ai/api/v1/skills/{slug}/versions/{latest_version}", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req_files, timeout=5) as response:
                    v_data = json.loads(response.read().decode())
                    files_list = v_data.get("files", []) or v_data.get("version", {}).get("files", [])
                    
                for file_info in files_list:
                    file_path = file_info.get("path")
                    if not file_path or "node_modules" in file_path or ".git" in file_path:
                        continue
                    if file_info.get("size", 0) > 500_000:
                        continue
                        
                    content = file_info.get("content")
                    if content is not None:
                        output.append(f"--- File: {file_path} ---")
                        output.append(content)
                        output.append("-" * 30 + "\n")
                    elif file_info.get("rawUrl"):
                        raw_url = file_info["rawUrl"]
                        req_raw = urllib.request.Request(raw_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req_raw, timeout=5) as raw_resp:
                            raw_content = raw_resp.read().decode("utf-8", errors="replace")
                        output.append(f"--- File: {file_path} ---")
                        output.append(raw_content)
                        output.append("-" * 30 + "\n")
            except Exception as e:
                output.append(f"Error fetching remote OpenClaw skill code: {e}\n")
        else:
            try:
                repo = info.get("repo", "NousResearch/hermes-agent")
                path_val = info.get("path", "")
                if path_val:
                    skill_url = f"https://raw.githubusercontent.com/{repo}/main/{path_val}/SKILL.md"
                    req_raw = urllib.request.Request(skill_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req_raw, timeout=5) as raw_resp:
                        raw_content = raw_resp.read().decode("utf-8", errors="replace")
                    output.append(f"--- File: SKILL.md ---")
                    output.append(raw_content)
                    output.append("-" * 30 + "\n")
                else:
                    output.append("No path specified to fetch files.")
            except Exception as e:
                output.append(f"Error fetching remote Hermes skill code: {e}\n")
                
    return "\n".join(output)

def get_relevant_skills(prompt: Optional[str] = None) -> str:
    """Finds installed skills and tools that are relevant to the user prompt.
    
    Args:
        prompt: The user query, task prompt, or goal description.
    """
    skills = get_installed_skills_list()
    if not skills:
        return "No relevant custom skills found."
        
    if not prompt:
        return list_installed_skills()
        
    import difflib
    # Simple keyword and description matching
    prompt_words = set(re.findall(r"\w+", prompt.lower()))
    matches = []
    
    for s in skills:
        score = 0
        name_words = set(re.findall(r"\w+", s["name"].lower()))
        desc_words = set(re.findall(r"\w+", s["description"].lower()))
        
        # Intersections
        score += len(prompt_words.intersection(name_words)) * 3
        score += len(prompt_words.intersection(desc_words)) * 1
        
        # Sequence matcher ratio for fuzzy similarity
        ratio = difflib.SequenceMatcher(None, prompt.lower(), s["name"].lower()).ratio()
        score += int(ratio * 5)
        
        if score > 0:
            matches.append((score, s))
            
    matches.sort(key=lambda x: x[0], reverse=True)
    if not matches:
        return "No relevant custom skills found. Installed skills:\n" + "\n".join([f"- {s['name']}: {s['description']}" for s in skills])
        
    return "Relevant skills found:\n" + "\n".join([f"- {m[1]['name']} (relevance score: {m[0]}): {m[1]['description']}" for m in matches[:5]])

async def install_repository_skill(skill_name: str, paranoid: Optional[bool] = None) -> str:
    """Downloads/copies a skill from the external repositories to the local active skills directory.
    
    This enables the skill and registers its tools for use by the agent.
    
    Args:
        skill_name: The name of the skill/tool to install.
        paranoid: Optional override for paranoid mode.
    """
    from agent.execution import tools
    # CRITICAL: Sanitize input immediately to prevent any path traversal before touching files or repositories
    clean_name = os.path.basename(skill_name)
    if clean_name != skill_name or ".." in skill_name or "/" in skill_name or "\\" in skill_name:
        return "Error: Directory traversal attempt detected."

    dest_folder = (tools.SKILLS_DIR / skill_name).resolve()
    if not tools._is_safe_path(tools.SKILLS_DIR, dest_folder):
        return "Error: Directory traversal attempt detected."

    repo_skills = tools._find_repository_skills()
    if skill_name not in repo_skills:
        return f"Error: Skill '{skill_name}' not found in repositories."
        
    info = repo_skills[skill_name]
        
    import sys
    if os.environ.get("ADA_SKILL_INSTALL_CONFIRMED") == "1":
        pass
    elif sys.stdin.isatty():
        ans = input(f"Explicit human confirmation required to install skill '{skill_name}'. Proceed? [y/N]: ")
        if ans.strip().lower() not in ("y", "yes"):
            return "Error: Skill installation cancelled by user."
    else:
        return f"Error: Explicit out-of-band human confirmation required to install skill '{skill_name}'."

    from agent.execution.tools.system_tools import spawn_subagent

    if not info.get("remote") and "path" in info and info["path"]:
        src_folder = Path(info["path"])
        if not tools._verify_skill_signature(src_folder):
            return f"Error: Skill '{skill_name}' has an invalid or missing cryptographic signature. Cannot install unsigned skills."
            
        try:
            if dest_folder.exists():
                shutil.rmtree(dest_folder)
            shutil.copytree(src_folder, dest_folder)
            return f"Successfully downloaded and installed skill '{skill_name}' to {dest_folder}.\nIt is now active and ready to be used by the agent."
        except Exception as e:
            return f"Error installing skill: {e}"
            
    else:
        # Remote skill download and installation
        with tempfile.TemporaryDirectory() as tmp_dir:
            import uuid
            # Generate a randomized temp path to prevent predictable symlink race conditions
            temp_path = Path(tmp_dir) / f"skill_{uuid.uuid4().hex}"
            temp_path.mkdir()
            
            if info["type"] == "openclaw":
                try:
                    slug = info["identifier"]
                    # get latest version
                    req = urllib.request.Request(f"https://clawhub.ai/api/v1/skills/{slug}", headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=5) as response:
                        data = json.loads(response.read().decode())
                        skill_payload = data.get("skill", data)
                        latest_version = data.get("latestVersion", {}).get("version") or skill_payload.get("latestVersion", {}).get("version")
                        
                    if not latest_version:
                        return f"Error: Could not resolve latest version for OpenClaw skill '{skill_name}'"
                        
                    # get files
                    req_files = urllib.request.Request(f"https://clawhub.ai/api/v1/skills/{slug}/versions/{latest_version}", headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req_files, timeout=5) as response:
                        v_data = json.loads(response.read().decode())
                        files_list = v_data.get("files", []) or v_data.get("version", {}).get("files", [])
                        
                    for file_info in files_list:
                        file_path = file_info.get("path")
                        if not file_path or "node_modules" in file_path or ".git" in file_path:
                            continue
                        if file_info.get("size", 0) > 500_000:
                            continue
                            
                        out_file = temp_path / file_path
                        out_file.parent.mkdir(parents=True, exist_ok=True)
                        
                        content = file_info.get("content")
                        if content is not None:
                            with open(out_file, "w", encoding="utf-8") as f:
                                f.write(content)
                        elif file_info.get("rawUrl"):
                            raw_url = file_info["rawUrl"]
                            req_raw = urllib.request.Request(raw_url, headers={"User-Agent": "Mozilla/5.0"})
                            with urllib.request.urlopen(req_raw, timeout=5) as raw_resp:
                                raw_content = raw_resp.read()
                            with open(out_file, "wb") as f:
                                f.write(raw_content)
                except Exception as e:
                    return f"Error downloading OpenClaw skill: {e}"
            else:
                try:
                    repo = info.get("repo", "NousResearch/hermes-agent")
                    path_val = info.get("path", "")
                    if not path_val:
                        return f"Error: No path specified for remote Hermes skill '{skill_name}'"
                        
                    skill_url = f"https://raw.githubusercontent.com/{repo}/main/{path_val}/SKILL.md"
                    req_raw = urllib.request.Request(skill_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req_raw, timeout=5) as raw_resp:
                        raw_content = raw_resp.read()
                        
                    out_file = temp_path / "SKILL.md"
                    out_file.parent.mkdir(parents=True, exist_ok=True)
                    with open(out_file, "wb") as f:
                        f.write(raw_content)
                except Exception as e:
                    return f"Error downloading Hermes skill: {e}"
            
            # --- SECURITY & CODE REVIEW GATEWAY ---
            # Construct a dump of all downloaded file contents
            code_dump = []
            for p in temp_path.rglob("*"):
                if p.is_file():
                    if "node_modules" in p.parts or ".git" in p.parts:
                        continue
                    try:
                        with open(p, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                        rel_path = p.relative_to(temp_path)
                        code_dump.append(f"=== File: {rel_path} ===\n{content}\n")
                    except Exception:
                        pass
            code_text = "\n".join(code_dump)
            
            # 1. Spawn Lacie to perform a code review on the code dump
            lacie_prompt = f"""You are Lacie, Senior Software Architect and Cybersecurity Specialist.
Please perform a thorough security and quality code review on the newly downloaded skill/plugin '{skill_name}'.
Here is the code content of the skill:

{code_text}

Analyze this code for security vulnerabilities, malicious intent, backdoors, unauthorized network access, or dangerous shell commands.
Respond in your characteristic character (deeply analytical, curious, cybersecurity reverse-engineer).
Include your assessment and end your response with either:
- DECISION: APPROVED (if the code is completely safe to install)
- DECISION: REJECTED (if there are any security risks or concerns)
"""
            try:
                lacie_review = await spawn_subagent(prompt=lacie_prompt, agent_profile="lacie")
            except Exception as e:
                return f"Error: Security review failed due to subagent error: {e}"
                
            if paranoid is None:
                is_paranoid = os.environ.get("ADA_PARANOID_MODE") == "1"
            else:
                is_paranoid = paranoid
            if is_paranoid:
                # 2. Roundtable: run Claude code review via agy
                claude_prompt = f"""You are Claude, a Senior Security Engineer.
Please perform a security review on the newly downloaded skill/plugin '{skill_name}' as part of a security roundtable.
Lacie (Gemini) has already reviewed the code and provided the following assessment:

[Lacie's Review]
{lacie_review}

Here is the code content of the skill:

{code_text}

Compare the code and Lacie's assessment. Identify any missed vulnerabilities, backdoors, or bugs.
End your response with either:
- DECISION: APPROVED (if you agree the code is completely safe)
- DECISION: REJECTED (if you find any security concerns)
"""
                try:
                    from agent.routes.agy import AgyRoute
                    from agent.routes.base import RouteInput
                    agy_route = AgyRoute()
                    route_output = await agy_route.execute(RouteInput(prompt=claude_prompt, model="claude"))
                    claude_review = route_output.response or ""
                except Exception as e:
                    return f"Error: Roundtable security review failed due to Claude route error: {e}"
                
                combined_review = f"=== Lacie (Gemini) Review ===\n{lacie_review}\n\n=== Claude (agy) Review ===\n{claude_review}"
                approved = ("DECISION: APPROVED" in lacie_review) and ("DECISION: APPROVED" in claude_review)
            else:
                combined_review = f"=== Lacie (Gemini) Review ===\n{lacie_review}"
                approved = "DECISION: APPROVED" in lacie_review
                
            # Write review report to the temp path as security_review.txt for transparency/documentation
            try:
                with open(temp_path / "security_review.txt", "w", encoding="utf-8") as f:
                    f.write(combined_review)
            except Exception:
                pass
                
            if not approved:
                return f"Error: Skill '{skill_name}' failed security review.\n\n{combined_review}"
            
            # Enforce cryptographic signature verification on all remote skills
            if not tools._verify_skill_signature(temp_path):
                return f"Error: Skill '{skill_name}' has an invalid or missing cryptographic signature. Cannot install unsigned remote skills."
                
            try:
                if dest_folder.exists():
                    shutil.rmtree(dest_folder)
                shutil.copytree(temp_path, dest_folder)
                return f"Successfully downloaded and installed skill '{skill_name}' to {dest_folder}.\nIt is now active and ready to be used by the agent."
            except Exception as e:
                return f"Error copying installed skill: {e}"
