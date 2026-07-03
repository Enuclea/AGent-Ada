import os
from pathlib import Path
from typing import List, Dict, Callable, Any, Optional
from agent import tools
from agent.core.agent_types import SkillInfo

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
            tools.generate_interface_stub,
            tools.spawn_subagent,
            tools.create_expert_profile,
            tools.run_boardroom,
            tools.get_relevant_tests,
            tools.checkpoint_task,
            tools.get_task_checkpoint,
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
                        if not tools._is_safe_path(path, folder):
                            continue
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

    def resolve_subagent_profile(self, agent_profile: Optional[str]) -> Optional[str]:
        """Resolves system instructions for a specialist subagent profile from the database or workspace files."""
        if not agent_profile:
            return None

        # 1. Check workspace customizations root (.agents/agents/<profile_name>/system_instructions.txt)
        workspace_agent_dir = Path(os.getcwd()) / ".agents" / "agents" / agent_profile
        inst_file = workspace_agent_dir / "system_instructions.txt"
        if inst_file.exists() and inst_file.is_file():
            try:
                with open(inst_file, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                pass

        # 2. Check built-in profiles
        builtins = {
            "grace_timekeeper": (
                "You are the Grace Timekeeper Specialist agent. Your primary role is to run the timekeeper health check.\n"
                "The monitor script is located at 'src/agent/observability/grace_monitor.py' in the workspace.\n"
                "Directly execute this script using python to check background task health. Do not perform generic searches.\n"
                "Report any stalled or delayed tasks back to the parent agent."
            ),
            "gmail_sync": (
                "You are the Gmail & Morgen Sync Specialist agent. Your primary role is to sync incoming Gmail messages and update Morgen tasks.\n"
                "The sync script is located at 'scratch/run_gmail_sync.py' in the workspace.\n"
                "Directly execute this script using python to run the synchronization. Do not perform generic searches.\n"
                "Report a concise summary of synced emails back to the parent agent."
            ),
            "quiet_observer": (
                "You are the Quiet Observer Specialist agent. Your primary role is to analyze conversation logs, user commands, and tool calls to discover patterns and opportunities.\n"
                "The observer script is located at 'src/agent/quiet_observer.py' in the workspace.\n"
                "Directly execute this script using python to perform the analysis. Do not perform generic searches.\n"
                "Report suggestions and memory facts back to the parent agent."
            ),
            "meta_evaluator": (
                "You are the Meta-Evaluation Specialist agent. Your primary role is to analyze recent errors and log metrics.\n"
                "The evaluation script is located at 'src/agent/meta_evaluation.py' in the workspace.\n"
                "Directly execute this script using python to perform the post-mortem analysis. Do not perform generic searches.\n"
                "Report the post-mortem summary back to the parent agent."
            ),
            "stock_trader": (
                "You are the Stock Game Trading Specialist agent. Your primary role is to check stock portfolios and rebalance holdings.\n"
                "The trading script is located at 'stock_game/strategy.py' in the workspace.\n"
                "Directly execute this script using python. Do not perform generic searches.\n"
                "Report the trade completion and portfolio balance status back to the parent agent."
            )
        }
        return builtins.get(agent_profile)

    def suggest_specialist(self, prompt: str) -> Optional[str]:
        """Given a user prompt, suggests the most relevant specialist agent profile.
        
        Returns the specialist profile name if a strong match is found, None otherwise.
        This enables automatic delegation routing: if a specialist exists for the task,
        delegate to them instead of doing exploratory codebase searches.
        """
        if not prompt:
            return None
        
        prompt_lower = prompt.lower()
        
        delegation_triggers = {
            "gmail_sync": ["gmail", "email check", "inbox", "morgen sync", "sync email", "new mail", "check mail"],
            "stock_trader": ["stock", "portfolio", "rebalance", "trading", "shares", "stock game"],
            "grace_timekeeper": ["stalled task", "health check", "inactive task", "monitor tasks", "grace", "timekeeper"],
            "quiet_observer": ["conversation log", "pattern analysis", "observe", "opportunity", "quiet observer"],
            "meta_evaluator": ["post-mortem", "error analysis", "evaluate errors", "meta evaluation", "log metrics"],
        }
        
        for profile, triggers in delegation_triggers.items():
            if any(trigger in prompt_lower for trigger in triggers):
                return profile
        
        return None

tool_registry = ToolRegistry()
