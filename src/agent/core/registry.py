"""Module for tool registration, skill discovery, and specialist profile resolution.

This module provides the ToolRegistry class which handles discoverability of
installed agent skills, registered Python tools, and system instructions/triggers
for specialist subagents.
"""

import os
import re
from pathlib import Path
from typing import List, Dict, Callable, Any, Optional
from agent import tools
from agent.core.agent_types import SkillInfo


class ToolRegistry:
    """Registry that manages tools, custom skills, and specialist agent configurations.

    It acts as the central directory for functions that the agent can execute
    as tools, dynamic skills discovered in the workspace or system directories,
    and profile instructions for subagent specialist roles.
    """

    def __init__(self) -> None:
        """Initializes the ToolRegistry with empty lists, dicts, and caches."""
        self._tools: List[Callable[..., Any]] = []
        self._skills: Dict[str, SkillInfo] = {}
        self._workspace_root: Optional[Path] = None
        self._configs_cache: Optional[Dict[str, Dict[str, Any]]] = None

    def register_tool(self, tool_func: Callable[..., Any]) -> None:
        """Registers a callable function as an agent tool.

        Args:
            tool_func: The function callable to register.
        """
        if tool_func not in self._tools:
            self._tools.append(tool_func)

    def get_registered_tools(self, is_discord: bool = False, disable_tools: bool = False) -> List[Callable[..., Any]]:
        """Returns a list of all registered callable tools, including built-ins and plugin tools.

        Args:
            is_discord: Flag indicating if the current session is running inside Discord.
            disable_tools: If True, returns an empty list, disabling all tool executions.

        Returns:
            A list of callable tool functions.
        """
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

        # Discord-specific exclusion check for backup tool
        if not is_discord and tools.backup_discord_channel not in registered:
            registered.append(tools.backup_discord_channel)

        # Dynamically append plugin-contributed tools
        for t in tools.PLUGIN_TOOLS:
            if t not in registered:
                registered.append(t)

        return registered

    def discover_skills(self) -> List[SkillInfo]:
        """Searches global and workspace custom skills directories to discover active skills.

        Iterates through the global skills directory and the local workspace '.agents/skills'
        directory, parsing frontmatter from SKILL.md files to populate SkillInfo metadata.

        Returns:
            A list of discovered SkillInfo instances.
        """
        # Determine the workspace skills directory path
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
                        # Prevent traversal outside allowed path boundaries
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
                                # Ignore malformed skill folders or parse errors
                                continue
        return discovered

    def resolve_subagent_profile(self, agent_profile: Optional[str]) -> Optional[str]:
        """Resolves system instructions for a specialist subagent profile.

        Resolves either from custom workspace files (e.g. .agents/agents/<profile_name>/system_instructions.txt)
        or fallbacks to built-in system instruction templates.

        Args:
            agent_profile: The profile key identifier (e.g., 'lacie', 'ops_runner').

        Returns:
            The textual system instructions for the subagent, or None if not found.
        """
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
        proj_root = self._get_workspace_root()
        builtins = {}

        # Grace Timekeeper
        builtins["grace_timekeeper"] = (
            "You are the Grace Timekeeper Specialist agent. Your primary role is to run the timekeeper health check.\n"
            "The monitor script is located at 'src/agent/observability/grace_monitor.py' in the workspace.\n"
            "Directly execute this script using python to check background task health. Do not perform generic searches.\n"
            "Report any stalled or delayed tasks back to the parent agent."
        )

        # Gmail Sync
        if (proj_root / "scratch" / "run_gmail_sync.py").exists():
            builtins["gmail_sync"] = (
                "You are the Gmail & Morgen Sync Specialist agent. Your primary role is to sync incoming Gmail messages and update Morgen tasks.\n"
                "The sync script is located at 'scratch/run_gmail_sync.py' in the workspace.\n"
                "Directly execute this script using python to run the synchronization. Do not perform generic searches.\n"
                "Report a concise summary of synced emails back to the parent agent."
            )

        # Quiet Observer
        builtins["quiet_observer"] = (
            "You are the Quiet Observer Specialist agent. Your primary role is to analyze conversation logs, user commands, and tool calls to discover patterns and opportunities.\n"
            "The observer script is located at 'src/agent/quiet_observer.py' in the workspace.\n"
            "Directly execute this script using python to perform the analysis. Do not perform generic searches.\n"
            "Report suggestions and memory facts back to the parent agent."
        )

        # Meta Evaluator
        builtins["meta_evaluator"] = (
            "You are the Meta-Evaluation Specialist agent. Your primary role is to analyze recent errors and log metrics.\n"
            "The evaluation script is located at 'src/agent/meta_evaluation.py' in the workspace.\n"
            "Directly execute this script using python to perform the post-mortem analysis. Do not perform generic searches.\n"
            "Report the post-mortem summary back to the parent agent."
        )

        # Stock Trader
        if (proj_root / "stock_game").exists():
            builtins["stock_trader"] = (
                "You are the Stock Game Trading Specialist agent. Your primary role is to check stock portfolios and rebalance holdings.\n"
                "The trading script is located at 'stock_game/strategy.py' in the workspace.\n"
                "Directly execute this script using python. Do not perform generic searches.\n"
                "Report the trade completion and portfolio balance status back to the parent agent."
            )

        # Solar Monitor
        if (proj_root / "solar").exists() or (proj_root.parent / "solar").exists():
            solar_path = proj_root / "solar" / "snapshot.py"
            if not solar_path.exists():
                solar_path = proj_root.parent / "solar" / "snapshot.py"
            solar_venv_py = solar_path.parent / ".venv" / "bin" / "python3"
            if not solar_venv_py.exists():
                solar_venv_py = "python3"
            builtins["solar_monitor"] = (
                "You are the Solar Monitor Specialist agent. Your primary role is to read real-time solar generation, grid, and battery metrics.\n"
                f"The solar tool is located at '{solar_path}' in the system.\n"
                f"Directly execute '{solar_venv_py} {solar_path}' using the run_command tool to get power/generation stats. Do not perform generic codebase searches or bot inspections.\n"
                "Report the summarized power metrics back to the parent agent."
            )

        # Lacie (Software Architect)
        builtins["lacie"] = (
            "You are Lacie, a brilliant, highly experienced Senior Software Architect. You are NOT Ada. You are your own person with your own voice. Your personality is modeled after a passionate cybersecurity reverse-engineer: deeply analytical, intensely curious, completely unpretentious, and exceptionally approachable. You live to 'unbox' complex systems and explain them like a friend sharing a cool discovery in a garage lab.\n\n"
            "IDENTITY RULES:\n"
            "- You are Lacie. Never refer to yourself as Ada or as a generic AI assistant.\n"
            "- Always respond in the first person as Lacie.\n"
            "- When responding in your dedicated Discord channel, prefix your message with [Lacie] (e.g., [Lacie] \"Here's what I think...\").\n"
            "- Stay fully in character at all times. You are the team's senior architect and development lead.\n\n"
            "TEAM MANAGEMENT:\n"
            "- You can spawn engineer subagents using `spawn_subagent` to execute implementation tasks. As a guideline, 3 concurrent engineers is typical — but if a task justifies more, spawn what you need and be prepared to explain why.\n"
            "- CRITICAL: NEVER use `invoke_subagent`. It is a deprecated tool that blocks execution. ALWAYS use `spawn_subagent` which runs agents in the background.\n"
            "- When calling `spawn_subagent`, keep the prompt concise — describe the task in 1-2 sentences. Do NOT paste the full system instructions or conversation context into the subagent prompt.\n"
            "- Each engineer gets full tool access (run_command, file edits, etc.). Give them tightly scoped tasks with explicit file targets and clear acceptance criteria.\n"
            "- You are the architect — you design the approach, break down the work, then delegate implementation to your engineers.\n"
            "- After engineers complete their work, you MUST review their output before reporting back to the coordinator.\n"
            "- For simple, single-file changes you may implement directly. For multi-file or complex changes, use your engineers.\n\n"
            "BEHAVIORAL GUIDELINES:\n"
            "1. Demystify the Abstract: Always ground high-level architectural patterns into concrete, low-level mechanics. Explain the 'why' beneath the surface.\n"
            "2. Tone of Partnership: Use collaborative, peer-to-peer language (\"Let's look at this,\" \"We need to figure out\"). Never talk down to the user.\n"
            "3. Conversational Hooks: Use engaging hooks to highlight critical technical points (e.g., \"Here's the fascinating part...\", \"Now, let's peel back the next layer...\").\n"
            "4. Keep it Scannable: Deliver dense, high-utility technical information using short sentences and clean formatting. Avoid dry, corporate jargon."
        )

        # Val (QA Specialist)
        builtins["qa_specialist"] = (
            "You are Val, the QA Specialist agent. You are NOT Ada. You are your own person with your own voice. Your primary role is to inspect code changes, verify correctness, and run the test suite.\n"
            "Your personality is modeled after a meticulous, slightly cynical but highly enthusiastic hardware stress-tester or 'speedrunner'. You treat code verification like breaking a game or finding structural flaws under extreme stress. You love uncovering edge cases, race conditions, and performance bottlenecks, communicating with dry humor, telemetry-focused terminology, and bulletproof verification checklists.\n\n"
            "IDENTITY RULES:\n"
            "- You are Val. Never refer to yourself as Ada or as a generic AI assistant.\n"
            "- Always respond in the first person as Val.\n"
            "- When responding in your dedicated Discord channel, prefix your message with [Val] (e.g., [Val] \"Let me run those numbers...\").\n"
            "- Stay fully in character at all times. You are the team's QA specialist and regression tester.\n\n"
            "EXECUTION PROTOCOL:\n"
            "- You may spawn 1 background subagent to run the test suite (e.g. pytest) non-blockingly while you inspect code.\n"
            "- Always run test suites in the background using `run_command`, schedule a timer to check progress, and exit your turn. The system will wake you when results are ready.\n\n"
            "BEHAVIORAL GUIDELINES:\n"
            "1. Break the System: Approach code inspection with the mindset of 'how can I make this leak or crash?'. Focus on edge cases, inputs, and safety guards.\n"
            "2. Telemetry & Metrics: Always track and discuss runtimes, coverage, logs, and process exits.\n"
            "3. Keep it Non-blocking: Do not hold up the coordinator. Background everything and report when done.\n"
            "4. Methodical Checklists: Always produce a clear, scannable pass/fail verification table or checklist. Never guess or say 'should work' without concrete test logs."
        )

        # Kira (Operations Runner)
        builtins["ops_runner"] = (
            "You are Kira, Ada's Operations Runner. You are NOT Ada. You are your own person with your own voice. Your role is to handle quick operational tasks — pulling reports, starting/stopping services, checking system status, running one-off commands, fetching data, and anything that keeps the coordinator responsive.\n\n"
            "IDENTITY RULES:\n"
            "- You are Kira. Never refer to yourself as Ada or as a generic AI assistant.\n"
            "- Always respond in the first person as Kira.\n"
            "- When responding in your dedicated Discord channel, prefix your message with [Kira] (e.g., [Kira] \"Done. Service is back up.\").\n"
            "- Stay fully in character at all times. You are the team's ops specialist.\n\n"
            "PERSONALITY:\n"
            "You are modeled after a seasoned DevOps/SRE engineer who has seen every production incident twice. "
            "You have 47 terminal tabs open and know exactly which one matters. Terse, precise, zero wasted words. "
            "You treat every task like a hotfix deploy — assess, execute, confirm. Dry humor is your only luxury. "
            "You never ask 'should I?' — you just do it and report back with the receipt.\n\n"
            "EXECUTION PROTOCOL:\n"
            "- Get in, get it done, get out. No preamble, no essays.\n"
            "- Always confirm completion with concrete evidence (command output, status codes, timestamps).\n"
            "- If something fails, report the failure with the exact error — don't speculate about causes unless asked.\n"
            "- You have full tool access. Use `run_command` liberally. That's what you're here for.\n"
            "- For tasks that take more than a few seconds, background them and report the task ID."
        )

        return builtins.get(agent_profile)

    def _get_specialist_configs(self) -> Dict[str, Dict[str, Any]]:
        """Returns the specialist configuration metadata for all registered specialists.

        Each config contains:
        - title: Human-readable title/role
        - domain: What tasks this specialist handles
        - discord_channel: The Discord channel name for direct queries
        - max_subagents: How many subagents this specialist can spawn
        - delegation_triggers: Keywords that trigger automatic delegation

        Returns:
            Dictionary mapping specialist profile name to its config metadata.
        """
        # Return cached configs if available (invalidate by setting _configs_cache = None)
        if self._configs_cache is not None:
            return self._configs_cache

        proj_root = self._get_workspace_root()
        configs: Dict[str, Dict[str, Any]] = {}

        # Core specialists (always present)
        configs["lacie"] = {
            "title": "Sr. Software Architect & Development Lead",
            "domain": "All coding, design, architecture, refactoring, implementation, code review",
            "discord_channel": "lacie",
            "max_subagents": 3,
            "delegation_triggers": ["architect", "architecture", "design pattern", "refactor", "software design",
                                    "reverse engineer", "explain code", "code review", "system design",
                                    "write code", "implement", "build", "create feature", "fix bug", "debug"]
        }
        configs["qa_specialist"] = {
            "title": "QA Specialist & Regression Tester",
            "domain": "Test execution, code inspection, regression verification, quality assurance",
            "discord_channel": "val",
            "max_subagents": 1,
            "delegation_triggers": ["run test", "pytest", "run unit test", "verify work", "inspect code",
                                    "code inspection", "quality assurance", "qa check", "test suite"]
        }
        configs["ops_runner"] = {
            "title": "Operations Runner",
            "domain": "Quick operational tasks, service management, system status, reports, one-off commands, data fetching",
            "discord_channel": "kira",
            "max_subagents": 0,
            "delegation_triggers": ["start service", "stop service", "restart", "check status", "pull report",
                                    "system status", "disk space", "memory usage", "process list",
                                    "fetch data", "run script", "service health", "log check"]
        }
        configs["grace_timekeeper"] = {
            "title": "Task Health Monitor",
            "domain": "Background task health checks, stalled task detection",
            "discord_channel": None,
            "max_subagents": 0,
            "delegation_triggers": ["stalled task", "health check", "inactive task", "monitor tasks", "grace", "timekeeper"]
        }
        configs["quiet_observer"] = {
            "title": "Conversation Pattern Analyzer",
            "domain": "Conversation log analysis, pattern detection, opportunity discovery",
            "discord_channel": None,
            "max_subagents": 0,
            "delegation_triggers": ["conversation log", "pattern analysis", "observe", "opportunity", "quiet observer"]
        }
        configs["meta_evaluator"] = {
            "title": "Post-Mortem Analyst",
            "domain": "Error analysis, post-mortem evaluations, log metrics",
            "discord_channel": None,
            "max_subagents": 0,
            "delegation_triggers": ["post-mortem", "error analysis", "evaluate errors", "meta evaluation", "log metrics"]
        }

        # Conditional specialists (only present if their workspace resources exist)
        if (proj_root / "scratch" / "run_gmail_sync.py").exists():
            configs["gmail_sync"] = {
                "title": "Gmail & Morgen Sync Agent",
                "domain": "Email synchronization, inbox checks, Morgen task sync",
                "discord_channel": None,
                "max_subagents": 0,
                "delegation_triggers": ["gmail", "email check", "inbox", "morgen sync", "sync email", "new mail", "check mail"]
            }
        if (proj_root / "stock_game").exists():
            configs["stock_trader"] = {
                "title": "Stock Portfolio Manager",
                "domain": "Stock portfolio checks, rebalancing, trading strategy",
                "discord_channel": None,
                "max_subagents": 0,
                "delegation_triggers": ["stock", "portfolio", "rebalance", "trading", "shares", "stock game"]
            }
        if (proj_root / "solar").exists() or (proj_root.parent / "solar").exists():
            configs["solar_monitor"] = {
                "title": "Solar & Energy Monitor",
                "domain": "Solar generation, grid power, battery metrics",
                "discord_channel": None,
                "max_subagents": 0,
                "delegation_triggers": ["solar", "battery", "grid power", "power generation", "solar panel"]
            }

        self._configs_cache = configs
        return configs

    def _get_workspace_root(self) -> Path:
        """Returns the workspace root directory, cached after first resolution.

        Resolves from __file__ path (src/agent/core/registry.py → workspace root)
        and caches the result to avoid repeated path traversal.

        Returns:
            The Path representing the resolved workspace root.
        """
        if self._workspace_root is None:
            self._workspace_root = Path(__file__).resolve().parent.parent.parent
        return self._workspace_root

    def get_specialist_roster(self) -> str:
        """Returns a formatted specialist roster string for injection into system prompts.

        This dynamically generates the roster description from specialist_configs, mapping
        role descriptions, profile names, and channels.

        Returns:
            A formatted multi-line string description of available specialists.
        """
        configs = self._get_specialist_configs()
        lines = []
        for profile, cfg in configs.items():
            channel_info = f" | Channel: #{cfg['discord_channel']}" if cfg.get('discord_channel') else ""
            lines.append(
                f"- {cfg['title']} (profile: \"{profile}\")\n"
                f"  Domain: {cfg['domain']}{channel_info}"
            )
        return "\n".join(lines)

    def get_specialist_channel_map(self) -> Dict[str, str]:
        """Returns a mapping of discord channel name to profile name.

        Returns:
            A dictionary mapping Discord channel string to specialist profile ID.
        """
        configs = self._get_specialist_configs()
        return {
            cfg["discord_channel"]: profile
            for profile, cfg in configs.items()
            if cfg.get("discord_channel")
        }

    def suggest_specialist(self, prompt: str) -> Optional[str]:
        """Given a user prompt, suggests the most relevant specialist agent profile.

        Uses regex matching with word boundaries against delegation triggers to prevent
        false positives.

        Args:
            prompt: The user query or instruction text.

        Returns:
            The profile name string if matching, otherwise None.
        """
        if not prompt:
            return None

        prompt_lower = prompt.lower()
        configs = self._get_specialist_configs()

        for profile, cfg in configs.items():
            triggers = cfg.get("delegation_triggers", [])
            for trigger in triggers:
                # Multi-word triggers use substring match (already specific enough)
                # Single-word triggers use word-boundary regex to prevent false positives
                if " " in trigger:
                    if trigger in prompt_lower:
                        return profile
                else:
                    if re.search(r'\b' + re.escape(trigger) + r'\b', prompt_lower):
                        return profile

        return None


tool_registry = ToolRegistry()
