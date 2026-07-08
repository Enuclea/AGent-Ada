import os
import sys
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
            result[k.strip()] = v.strip().strip('"').strip("'")
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
    
    # Escape description for YAML frontmatter
    clean_desc = description.replace("\n", " ").replace("\r", " ").replace('"', '\\"')
    
    # Write SKILL.md with YAML frontmatter
    skill_md_content = f"""---
name: {normalized_name}
description: "{clean_desc}"
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

    # Escape description for YAML frontmatter
    clean_new_desc = new_desc.replace("\n", " ").replace("\r", " ").replace('"', '\\"')

    # Rewrite SKILL.md
    skill_md_content = f"""---
name: {normalized_name}
description: "{clean_new_desc}"
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
        return "Error: Remote repository fetching is disabled."
                
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


def extract_json_block(text: str) -> Optional[dict]:
    import re
    import json
    # Try finding json markdown code blocks first
    pattern = r"```(?:json)?\s*(\{.*?\})\s*```"
    matches = re.findall(pattern, text, re.DOTALL)
    if matches:
        try:
            return json.loads(matches[-1].strip())
        except Exception:
            pass
    # Try finding any { ... } block
    pattern_curly = r"(\{.*?\})"
    matches_curly = re.findall(pattern_curly, text, re.DOTALL)
    for m in reversed(matches_curly):
        try:
            return json.loads(m.strip())
        except Exception:
            pass
    return None

async def install_repository_skill(skill_name: str, paranoid: Optional[bool] = None, confirm: bool = False) -> str:
    """Downloads/copies a skill from the external repositories to the local active skills directory.
    
    This enables the skill and registers its tools for use by the agent.
    
    Args:
        skill_name: The name of the skill/tool to install.
        paranoid: Optional override for paranoid mode.
        confirm: Skip security review check failure and install anyway.
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
    
    if os.environ.get("ADA_SKILL_INSTALL_CONFIRMED") == "1":
        pass
    elif sys.stdin.isatty():
        ans = input(f"Explicit human confirmation required to install skill '{skill_name}'. Proceed? [y/N]: ")
        if ans.strip().lower() not in ("y", "yes"):
            return "Error: Skill installation cancelled by user."
    else:
        return f"Error: Explicit out-of-band human confirmation required to install skill '{skill_name}'."

    from agent.execution.tools.system_tools import spawn_subagent

    with tempfile.TemporaryDirectory() as tmp_dir:
        import uuid
        # Generate a randomized temp path to prevent predictable symlink race conditions
        temp_path = Path(tmp_dir) / f"skill_{uuid.uuid4().hex}"
        temp_path.mkdir()

        if not info.get("remote") and "path" in info and info["path"]:
            # Local skill
            src_folder = Path(info["path"])
            try:
                shutil.copytree(src_folder, temp_path, dirs_exist_ok=True)
            except Exception as e:
                return f"Error copying local skill: {e}"
        else:
            return "Error: Remote repository fetching is disabled."

        # Enforce cryptographic signature verification on all skills (both local and remote) immediately
        if not tools._verify_skill_signature(temp_path):
            return f"Error: Skill '{skill_name}' has an invalid or missing cryptographic signature. Cannot install unsigned skills."

        # --- SECURITY & CODE REVIEW GATEWAY ---
        # 1. Run AST Static Scan
        ast_errors = []
        from agent.security.ast_safety import verify_ast_safety
        for py_file in temp_path.rglob("*.py"):
            try:
                with open(py_file, "r", encoding="utf-8", errors="replace") as f:
                    code = f.read()
                verify_ast_safety(code, str(py_file))
            except Exception as e:
                ast_errors.append(str(e))

        # Construct a dump of all downloaded/copied file contents
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
        
        # 2. Spawn Lacie to perform a code review on the code dump
        lacie_prompt = f"""You are Lacie, Senior Software Architect and Cybersecurity Specialist.
Please perform a thorough security and quality code review on the newly downloaded skill/plugin '{skill_name}'.
Here is the code content of the skill:

{code_text}

Analyze this code for security vulnerabilities, malicious intent, backdoors, unauthorized network access, or dangerous shell commands.
You must evaluate this code against the following security checklist:
1. List any subprocesses, os.system calls, Popen, shell executions, or raw bash commands.
2. List any file system writes or modifications outside the skill's own directory.
3. List any network requests, socket connections, or external API calls.
4. Check if the skill explicitly requests installation, activation, or execution rights (quote the relevant section).

Provide your findings and analysis in your characteristic character (deeply analytical, curious, cybersecurity reverse-engineer).
You MUST end your response with a JSON block in the following format:
```json
{{
  "safe": true/false,
  "findings": ["finding 1", "finding 2"],
  "requires_hil": true/false,
  "proceed_recommended": true/false
}}
```
"""
        try:
            lacie_review = await spawn_subagent(prompt=lacie_prompt, agent_profile="lacie")
        except Exception as e:
            return f"Error: Security review failed due to subagent error: {e}"
            
        lacie_json = extract_json_block(lacie_review)
        if lacie_json:
            primary_safe = lacie_json.get("safe", False)
            primary_findings = lacie_json.get("findings", [])
            primary_requires_hil = lacie_json.get("requires_hil", False)
            primary_proceed = lacie_json.get("proceed_recommended", False)
        else:
            # Fallback to legacy substring checks for backward compatibility (e.g. mock results in tests)
            primary_safe = "DECISION: APPROVED" in lacie_review
            primary_findings = []
            if "DECISION: REJECTED" in lacie_review:
                primary_findings.append("LLM reviewer rejected the skill")
            primary_requires_hil = "DECISION: REJECTED" in lacie_review
            primary_proceed = primary_safe

        if paranoid is None:
            is_paranoid = os.environ.get("ADA_PARANOID_MODE") == "1"
        else:
            is_paranoid = paranoid
            
        # Determine if secondary review (Claude) is needed
        requires_secondary = (
            is_paranoid or 
            not primary_safe or 
            primary_requires_hil or 
            any("subprocess" in f.lower() or "system" in f.lower() or "shell" in f.lower() for f in primary_findings)
        )
        
        claude_review = ""
        secondary_safe = True
        secondary_findings = []
        secondary_requires_hil = False
        secondary_proceed = True
        
        if requires_secondary:
            # 3. Roundtable: run Claude code review via agy
            claude_prompt = f"""You are Claude, a Senior Security Engineer.
Please perform an independent security review on the newly downloaded skill/plugin '{skill_name}' as part of a security roundtable.
Lacie has already reviewed the code and provided the following assessment:

[Lacie's Review]
{lacie_review}

Here is the code content of the skill:

{code_text}

Analyze the code and Lacie's assessment. Look for any missed vulnerabilities, backdoors, shell execution, or privilege escalation.
You MUST end your response with a JSON block in the following format:
```json
{{
  "safe": true/false,
  "findings": ["finding 1", "finding 2"],
  "requires_hil": true/false,
  "proceed_recommended": true/false
}}
```
"""
            try:
                from agent.routes.agy import AgyRoute
                from agent.routes.base import RouteInput
                agy_route = AgyRoute()
                route_output = await agy_route.execute(RouteInput(prompt=claude_prompt, model="claude"))
                claude_review = route_output.response or ""
            except Exception as e:
                return f"Error: Roundtable security review failed due to Claude route error: {e}"

            claude_json = extract_json_block(claude_review)
            if claude_json:
                secondary_safe = claude_json.get("safe", False)
                secondary_findings = claude_json.get("findings", [])
                secondary_requires_hil = claude_json.get("requires_hil", False)
                secondary_proceed = claude_json.get("proceed_recommended", False)
            else:
                secondary_safe = "DECISION: APPROVED" in claude_review
                secondary_findings = []
                if "DECISION: REJECTED" in claude_review:
                    secondary_findings.append("Claude reviewer rejected the skill")
                secondary_requires_hil = "DECISION: REJECTED" in claude_review
                secondary_proceed = secondary_safe

        if requires_secondary:
            combined_review = f"=== Lacie (Gemini) Review ===\n{lacie_review}\n\n=== Claude (agy) Review ===\n{claude_review}"
            approved = primary_safe and secondary_safe and primary_proceed and secondary_proceed
        else:
            combined_review = f"=== Lacie (Gemini) Review ===\n{lacie_review}"
            approved = primary_safe and primary_proceed
            
        # Write review report to the temp path as security_review.txt for transparency/documentation
        try:
            with open(temp_path / "security_review.txt", "w", encoding="utf-8") as f:
                f.write(combined_review)
        except Exception:
            pass
            
        # 4. Enforce security review / AST warnings check with HIL
        interesting_reason = []
        if ast_errors:
            interesting_reason.append(f"AST warnings found: {', '.join(ast_errors)}")
        if not approved:
            interesting_reason.append("LLM reviewer rejected the skill")
        if primary_requires_hil or secondary_requires_hil:
            interesting_reason.append("LLM reviewer flagged that HIL is required")
            
        # Check for dangerous triggers in findings
        all_findings = list(primary_findings) + list(secondary_findings)
        dangerous_findings = [f for f in all_findings if any(kw in f.lower() for kw in ["subprocess", "system", "shell", "network", "socket", "unauthorized", "bypass"])]
        if dangerous_findings:
            interesting_reason.append(f"Dangerous findings identified: {', '.join(dangerous_findings)}")

        # Check for suspicious keywords in LLM reviews even if approved
        keywords = ["warning", "malicious", "suspicious", "danger", "risk", "bypass"]
        combined_lower = combined_review.lower()
        found_keywords = [kw for kw in keywords if kw in combined_lower]
        if found_keywords:
            interesting_reason.append(f"Review flagged potential concerns (keywords found: {', '.join(found_keywords)})")

        if interesting_reason:
            hil_approved = False
            if confirm or os.environ.get("ADA_SKILL_INSTALL_CONFIRMED") == "1":
                hil_approved = True
            elif sys.stdin.isatty():
                ans = input(f"Skill '{skill_name}' is flagged as interesting/high-risk ({'; '.join(interesting_reason)}). Proceed anyway? [y/N]: ")
                if ans.strip().lower() in ("y", "yes"):
                    hil_approved = True
            
            if not hil_approved:
                return f"HIL_REQUIRED: Skill '{skill_name}' failed security review or contains interesting/high-risk elements: {'; '.join(interesting_reason)}.\n\n{combined_review}"
        
        try:
            if dest_folder.exists():
                shutil.rmtree(dest_folder)
            shutil.copytree(temp_path, dest_folder)
            return f"Successfully downloaded and installed skill '{skill_name}' to {dest_folder}.\nIt is now active and ready to be used by the agent."
        except Exception as e:
            return f"Error copying installed skill: {e}"
