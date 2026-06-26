import os
from pathlib import Path
from typing import List, Dict, Callable, Any
from agent import tools
from agent.types import SkillInfo

class ToolRegistry:
    def __init__(self) -> None:
        self._tools: List[Callable[..., Any]] = []
        self._skills: Dict[str, SkillInfo] = {}
        
    def register_tool(self, tool_func: Callable[..., Any]) -> None:
        if tool_func not in self._tools:
            self._tools.append(tool_func)
            
    def get_registered_tools(self, is_discord: bool = False, disable_tools: bool = False) -> List[Callable[..., Any]]:
        if disable_tools:
            return []
            
        registered = list(self._tools)
        # Add default built-in tools if they are not already registered
        builtins = [
            tools.record_memory_fact,
            tools.record_memory_key_value,
            tools.create_agent_skill,
            tools.improve_agent_skill,
            tools.list_installed_skills,
            tools.search_past_conversations,
            tools.youtube_to_mp3,
            tools.schedule_task,
            tools.list_scheduled_tasks,
            tools.delete_scheduled_task,
            tools.run_command,
        ]
        for t in builtins:
            if t not in registered:
                registered.append(t)
                
        if not is_discord and tools.backup_discord_channel not in registered:
            registered.append(tools.backup_discord_channel)
            
        # Add plugin tools
        for t in tools.PLUGIN_TOOLS:
            if t not in registered:
                registered.append(t)
                
        return registered

    def discover_skills(self) -> List[SkillInfo]:
        # Search global and workspace custom skills directories
        workspace_skills = Path(os.getcwd()) / ".agents" / "skills"
        skills_paths = [tools.SKILLS_DIR]
        if workspace_skills.exists() and workspace_skills.is_dir():
            skills_paths.append(workspace_skills)
            
        discovered = []
        seen_paths = set()
        for path in skills_paths:
            if path.exists() and path.is_dir():
                for folder in path.iterdir():
                    if folder.is_dir():
                        folder_resolved = str(folder.resolve())
                        if folder_resolved in seen_paths:
                            continue
                        seen_paths.add(folder_resolved)
                        
                        skill_md = folder / "SKILL.md"
                        if skill_md.exists() and skill_md.is_file():
                            try:
                                with open(skill_md, "r", encoding="utf-8") as f:
                                    content = f.read()
                                fm = tools._parse_frontmatter(content)
                                name = fm.get("name", folder.name)
                                desc = fm.get("description", "No description.")
                                discovered.append(SkillInfo(
                                    name=name,
                                    description=desc,
                                    path=folder_resolved,
                                    instructions=content,
                                    author=fm.get("author"),
                                    version=fm.get("version")
                                ))
                            except Exception:
                                continue
        return discovered

tool_registry = ToolRegistry()
