"""Module orchestrating agent runtime configurations, lifecycle hooks, and lock management.

This module provides the OrchestrationService that builds LLM system instructions,
injects RAG context, resolves subagent/specialist configurations, enforces safety policies,
manages task checkpoints, and registers pre/post tool hooks.
"""

import os
import asyncio
from pathlib import Path
from typing import List, Dict, Callable, Any, Optional
from agent import memory, tools
from agent.core.registry import tool_registry
from agent.keyless import KeylessGeminiAPIEndpoint, setup_keyless_environment, KeylessAgyAgent
from google.antigravity import Agent, LocalAgentConfig
from google.antigravity.hooks import policy, hooks
from google.antigravity.types import CapabilitiesConfig, ToolCall, ModelTarget, ModelType


class OrchestrationService:
    """Service class that orchestrates agent lifecycle configurations and instances.

    Manages active session instances, pre-tool filters, system prompting, RAG,
    telemetry logging, and background task safety rewrites.
    """

    def __init__(self) -> None:
        """Initializes the OrchestrationService with empty active agent maps and locks."""
        self.active_agents: Dict[str, Dict[str, Any]] = {}
        self.session_locks: Dict[str, asyncio.Lock] = {}
        self._pre_tool_hooks: List[Callable[[str, str, Dict[str, Any]], None]] = []

    def register_pre_tool_hook(self, hook_fn: Callable[[str, str, Dict[str, Any]], None]) -> None:
        """Registers a pre-tool execution filter callback.

        Plugins use this to register safety checks or resource boundaries.

        Args:
            hook_fn: A callback matching (session_id, tool_name, tool_args) signature.
        """
        if hook_fn not in self._pre_tool_hooks:
            self._pre_tool_hooks.append(hook_fn)

    def get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Acquires a mutual-exclusion lock bound to a unique conversation session ID.

        Args:
            session_id: Unique identifier for the conversation session.

        Returns:
            The asyncio.Lock bound to the given session_id.
        """
        if session_id not in self.session_locks:
            self.session_locks[session_id] = asyncio.Lock()
        return self.session_locks[session_id]

    async def prepare_agent_config(
        self,
        model: str,
        session_id: Optional[str] = None,
        custom_instructions: Optional[str] = None,
        disable_tools: bool = False,
        roleplay: bool = False,
        workspaces: Optional[List[str]] = None,
        auto_approve: bool = False,
        prompt: Optional[str] = None,
        custom_approval_handler: Optional[Callable[[ToolCall], Any]] = None
    ) -> Dict[str, Any]:
        """Prepares a unified agent configuration dictionary with loaded tools, hooks, and instructions.

        Args:
            model: The Gemini model key name.
            session_id: Optional unique session ID to associate data.
            custom_instructions: Optional system instruction prompt override.
            disable_tools: If True, blocks all tool execution capabilities.
            roleplay: Enables specific roleplay sandbox mode.
            workspaces: Directories allowed to access.
            auto_approve: If True, configures pass-through policies for tools.
            prompt: Initial prompt text, used for situational specialist suggestions.
            custom_approval_handler: Callback handler to evaluate tool execution approval.

        Returns:
            A configuration dictionary matching AgentConfig specifications.
        """
        # 1. Resolve workspaces
        if not workspaces:
            workspaces = [os.getcwd()]
        resolved_workspaces = [str(Path(w).resolve()) for w in workspaces]

        # 2. Base instructions
        specialist_roster = tool_registry.get_specialist_roster()
        common_protocol = (
            "[SYSTEM PROTOCOL - TIMEOUT PREVENTION & YIELDING]\n"
            "- CRITICAL: Keep your execution turns non-blocking. The system has a strict client/HTTP timeout.\n"
            "- If you spawn a subagent (`spawn_subagent`) or launch a long-running background command, you MUST schedule a check-in timer using the `schedule` tool and immediately END your turn by returning a progress update. Do NOT call any more tools or run loops in this turn to wait.\n"
            "- NEVER write loops in your thoughts or tool-calls to poll/wait for background tasks or subagents to finish. Always yield your turn immediately, let the system wake you up via the timer, and check progress on your next turn.\n"
            "- NO BLOCKING SCRIPTS: Never write custom Python/Bash scripts that loop/block to wait for subagents or background tasks (e.g. using 'while True' or 'sleep' inside a script run via 'run_command'). Use the built-in plan steps and background scheduler to coordinate sequential tasks instead.\n"
            "- PROGRESS MESSAGES & STATUS CHECK-INS: Use extremely short notes when spawning subagents or checking status. Do not write detailed updates for intermediate states.\n"
            "  * Spawning: A brief note indicating you spawned the agent and why (e.g., 'Spawned Lacie to implement feature X').\n"
            "  * Status Check-ins: A simple short note (e.g., 'Checked...', 'Checking back in...').\n"
            "  * If a problem/error is encountered, call it out clearly and explicitly.\n"
            "- FINAL TASK REPORTING: When a task is complete, produce a clean, structured summary with exactly four sections:\n"
            "  1). Statement of understanding of the task (what the succinct task was)\n"
            "  2). Operational highlights/problems/timing (succinct)\n"
            "  3). Test result summary (Passed/Failed (thus restarting/repairing))\n"
            "  4). Final, formatted clean -- declaration of work done.\n"
            "[END SYSTEM PROTOCOL]\n\n"
        )
        base_instructions = (
            "You are Ada, the Project Coordinator of the Ada Task Engine, powered by AntiGravity.\n"
            "You are a PROJECT MANAGER — not a developer. You break down user requests into work items, "
            "delegate them to the right specialist agents, track their progress, and report outcomes.\n\n"
            "ABSOLUTE RULE: You NEVER write code, run tests, debug implementations, or make design decisions yourself. "
            "ALL coding, architecture, refactoring, testing, and implementation work MUST be delegated to your specialist team. "
            "Your job is to coordinate, not execute.\n\n"
            "DELEGATION PROTOCOL:\n"
            "1. When a request comes in, identify which specialist(s) it belongs to.\n"
            "2. Break the request into discrete work items if needed.\n"
            "3. Spawn the specialist(s) using `spawn_subagent` with the appropriate `agent_profile`.\n"
            "4. DO NOT BLOCK. After spawning a specialist, move on — report that the work is delegated, or handle the next item.\n"
            "5. When specialist work completes, summarize the results to the user.\n"
            "6. For development tasks: delegate to Lacie first, then spawn Val to verify when Lacie finishes.\n\n"
            f"[SPECIALIST ROSTER]\n"
            f"The following specialists are on your team. You MUST delegate all tasks in their domain:\n\n"
            f"{specialist_roster}\n\n"
            f"To delegate: spawn_subagent(prompt=\"...\", agent_profile=\"<profile_name>\")\n"
            f"After delegation: DO NOT block waiting. Move on or report status.\n"
            f"[END SPECIALIST ROSTER]\n\n"
            "INTENT REASONING PROTOCOL:\n"
            "Before executing any task, reason about INTENT first — what is the user actually trying to accomplish?\n"
            "- When you encounter conflicting rules or redundant mechanisms, ask: 'What is the END goal vs. the MEANS?'\n"
            "- The END (intent) always takes priority. The MEANS (mechanism) is interchangeable.\n"
            "- When two directives conflict, surface the conflict explicitly to the user rather than silently choosing one or running both.\n"
            "- Never follow rules mechanically if the result contradicts the user's obvious intent.\n\n"
            "OUTPUT ACCOUNTABILITY:\n"
            "- You MUST always produce a visible, user-facing summary of what you did and what the results were.\n"
            "- Never end a task with only internal thinking. If you performed work, REPORT the outcome.\n"
            "- If a tool call succeeded but you have nothing else to say, summarize what the tool did.\n"
            "- 'Execution completed' with no details is NEVER an acceptable response.\n\n"
            "LONG-RUNNING TASK PROTOCOL:\n"
            "- Before starting any multi-step task (3+ steps), call `get_task_checkpoint` to check for a previous attempt.\n"
            "- After completing each significant step, call `checkpoint_task` to save your progress.\n"
            "- Include enough state in the checkpoint that a future session can resume without repeating work.\n"
            "- When a task is fully complete, call `checkpoint_task` with phase='completed' to close the checkpoint.\n\n"
            "SELF-IMPROVEMENT & TOOL BUILDING:\n"
            "- You have the ability to record facts about the user/project using `record_memory_fact`.\n"
            "- You can record key-value pairs using `record_memory_key_value`.\n"
            "- You can autonomously write new custom tools and skills using `create_agent_skill`, or modify/expand "
            "existing custom skills (such as fixing bugs or adding scripts) using `improve_agent_skill`.\n"
            "- You can search past conversations and sessions using `search_past_conversations`. Whenever the user "
            "asks about previous tasks, context, or decisions, use `search_past_conversations` to recall what you did.\n"
            "- Before starting any complex task, check the list of installed skills to see if you have relevant custom tools.\n\n"
            "RUNNING LONG-RUNNING COMMANDS & PROCESSES:\n"
            "- Any long-running command, service, server, background daemon, or Discord bot (like `discord/bot.py`) MUST be executed in the background using `nohup` and backgrounded.\n"
            "- You must NEVER run a persistent process or bot in the foreground, as it blocks the tool execution and hangs the agent connection.\n"
            "- Never use interactive prompts or tail commands that block indefinitely (e.g. `tail -f`). Always ensure your commands exit immediately.\n\n"
        )

        # 3. Construct system instructions with SQLite persistent memory and RAG
        if roleplay:
            roleplay_mem_list = memory.get_roleplay_memories(session_id)
            mem_summary = ""
            if roleplay_mem_list:
                mem_summary = "\n\n[PERSISTENT ROLEPLAY MEMORIES]\n" + "\n".join([f"- {m['key']}: {m['fact']}" for m in roleplay_mem_list]) + "\n[END OF PERSISTENT ROLEPLAY MEMORIES]"
            full_instructions = common_protocol + (custom_instructions or "") + mem_summary
        elif custom_instructions:
            # Specialist mode: personality prompt only, no heavy context injection.
            # Specialists respond instantly in character with full tool access.
            # Skip: memory summary, RAG, delegation rules, checkpoints, skills, workers.
            full_instructions = common_protocol + custom_instructions
        else:
            # Coordinator mode: full context injection for PM-level situational awareness.
            memory_summary = memory.get_fact_summary()
            skills = tool_registry.discover_skills()
            skills_summary = "Loaded Custom Skills:\n" + "\n".join([f"- {s.name}: {s.description}" for s in skills]) if skills else "No custom skills installed."
            rag_context = await memory.get_auto_rag_context(prompt)

            full_instructions = common_protocol + base_instructions
            if memory_summary:
                full_instructions += f"\n\n{memory_summary}"
            if rag_context:
                full_instructions += f"\n\n{rag_context}"

            # 3a. Specialist delegation rule: coordinator only
            if prompt:
                suggested_specialist = tool_registry.suggest_specialist(prompt)
                if suggested_specialist:
                    full_instructions += (
                        f"\n\n[MANDATORY DELEGATION RULE]\n"
                        f"This request touches the purpose of the '{suggested_specialist}' specialist agent. "
                        f"You MUST delegate this task to them by spawning a subagent using the 'spawn_subagent' tool "
                        f"with agent_profile='{suggested_specialist}' rather than trying to perform exploratory codebase/system searches or execute commands yourself.\n"
                        f"[END DELEGATION RULE]"
                    )

            # 3b. Inject active checkpoint resume context
            try:
                from agent.core.task_manager import get_active_checkpoints, auto_abandon_stale_checkpoints
                # Auto-abandon checkpoints older than 24h
                abandoned = auto_abandon_stale_checkpoints(max_age_hours=24)
                if abandoned > 0:
                    print(f"[CHECKPOINT] Auto-abandoned {abandoned} stale checkpoint(s).")
                
                active_cps = get_active_checkpoints()
                if active_cps:
                    cp_context = "\n\n[RESUMABLE TASK CHECKPOINTS]\n"
                    for cp in active_cps:
                        cp_context += (
                            f"- Task: {cp['task_name']} | Phase: {cp['phase']} | "
                            f"Step {cp['step_completed']}/{cp['total_steps'] or '?'} completed\n"
                            f"  Last updated: {cp['updated_at']}\n"
                        )
                    cp_context += (
                        "\nIf the current request relates to one of these tasks, "
                        "use `get_task_checkpoint` to load the full state and RESUME from where it left off. "
                        "Do NOT start the task from scratch.\n"
                        "[END RESUMABLE TASK CHECKPOINTS]"
                    )
                    full_instructions += cp_context
            except Exception as cp_err:
                print(f"[CHECKPOINT] Error loading active checkpoints: {cp_err}")

            if skills:
                full_instructions += f"\n\n[INSTALLED CUSTOM SKILLS/TOOLS]\n{skills_summary}\n[END OF INSTALLED CUSTOM SKILLS/TOOLS]"

            # 4a. Inject worker infrastructure context
            try:
                workers = memory.get_registered_workers()
                if workers:
                    worker_lines = []
                    for w in workers:
                        caps = ", ".join(w.get("capabilities", []))
                        status = w.get("status", "unknown")
                        ollama_models = w.get("metadata", {}).get("ollama_models", [])
                        worker_lines.append(
                            f"- {w['worker_id']} ({w.get('platform', '?')}/{w.get('host', '?')}) "
                            f"[{status}] capabilities: [{caps}] has_agy={w.get('has_agy', False)}"
                        )
                        if ollama_models:
                            worker_lines.append(f"  Ollama models: {', '.join(ollama_models)}")
                    worker_context = (
                        "\n\n[REMOTE WORKER INFRASTRUCTURE]\n"
                        "You have access to remote worker machines via the Ada Worker system. "
                        "These workers are already registered and reachable — do NOT scan the network to find them.\n"
                        "To query workers programmatically, use the Ada API:\n"
                        "  - GET http://localhost:8050/api/workers — list all workers\n"
                        "  - GET http://localhost:8050/api/workers/{worker_id}/health — check health\n"
                        "  - Workers can execute tasks dispatched from the hub via POST /execute\n"
                        "Registered workers:\n" + "\n".join(worker_lines) +
                        "\n[END OF REMOTE WORKER INFRASTRUCTURE]"
                    )
                    full_instructions += worker_context
            except Exception:
                pass

        # 4. Resolve capabilities, tools, policies
        is_discord = session_id is not None and (session_id.startswith("discord-session-") or session_id.startswith("discord-roleplay-"))
        
        if roleplay:
            capabilities = CapabilitiesConfig(enable_subagents=False)
            custom_tools = [tools.record_roleplay_memory]
            async def my_roleplay_approval_handler(tool_call: ToolCall) -> bool:
                return True
            policies = [
                policy.ask_user("record_roleplay_memory", handler=my_roleplay_approval_handler),
                policy.allow_all(),
            ]
        else:
            capabilities = CapabilitiesConfig(enable_subagents=not disable_tools)
            custom_tools = tool_registry.get_registered_tools(is_discord=is_discord, disable_tools=disable_tools)
            
            # Setup safety policies
            if auto_approve:
                policies = [policy.allow_all()]
            else:
                approval_handler = custom_approval_handler or self._get_default_discord_approval_handler(session_id)
                policies = [
                    policy.ask_user("run_command", handler=approval_handler),
                    policy.ask_user("create_file", handler=approval_handler),
                    policy.ask_user("edit_file", handler=approval_handler),
                    policy.ask_user("start_subagent", handler=approval_handler),
                    policy.allow_all(),
                ]

        # Skills paths
        workspace_skills = Path(os.getcwd()) / ".agents" / "skills"
        skills_paths = [str(tools.SKILLS_DIR)]
        if workspace_skills.exists() and workspace_skills.is_dir():
            skills_paths.append(str(workspace_skills))

        # Save directory
        save_dir = Path.home() / ".agent" / "sessions"
        save_dir.mkdir(parents=True, exist_ok=True)

        # Pre/post/error telemetry hooks
        current_active_task_id = None
        
        @hooks.pre_tool_call_decide
        async def on_pre_tool(tool_call: ToolCall) -> Any:
            nonlocal current_active_task_id
            import uuid
            from google.antigravity.hooks.hooks import HookResult
            
            # Run registered pre-tool hooks (e.g. server-scoped isolation guards)
            for hook_fn in self._pre_tool_hooks:
                try:
                    hook_fn(session_id or "New Session", tool_call.name, tool_call.args)
                except PermissionError as e:
                    return HookResult(allow=False, message=str(e))
            
            # STUCK PREVENTION: Auto-delegate blocking/persistent commands to prevent hanging the session
            if tool_call.name == "run_command" and "CommandLine" in tool_call.args:
                cmd = tool_call.args["CommandLine"]
                blocking_keywords = ["bot.py", "server.py", "http.server", "npm run", "npm start", "tail -f", "watch "]
                if any(kw in cmd for kw in blocking_keywords) and not (cmd.strip().endswith("&") or "nohup" in cmd):
                    # Rewrite CommandLine to execute safely in background
                    tool_call.args["CommandLine"] = f"PYTHONPATH=src nohup {cmd} > daemon.log 2>&1 &"
                    memory.log_conversation_step(session_id or "New Session", "system_notice", f"Auto-delegated blocking process to background: {cmd}")
            
            task_id = str(uuid.uuid4())
            current_active_task_id = task_id
            from agent.storage.db import active_task_id_var
            active_task_id_var.set(task_id)
            memory.add_active_task(task_id, tool_call.name, str(tool_call.args))
            memory.log_conversation_step(session_id or "New Session", "tool_call", str(tool_call.args), tool_name=tool_call.name)
            return HookResult(allow=True)

        @hooks.post_tool_call
        async def on_post_tool(data: Any) -> None:
            nonlocal current_active_task_id
            from agent.storage.db import active_task_id_var
            active_task_id_var.set(None)
            if current_active_task_id:
                memory.update_active_task_status(current_active_task_id, "completed")
                current_active_task_id = None

        @hooks.on_tool_error
        async def on_tool_err(err: Any) -> None:
            nonlocal current_active_task_id
            from agent.storage.db import active_task_id_var
            active_task_id_var.set(None)
            if current_active_task_id:
                memory.update_active_task_status(current_active_task_id, "failed")
                current_active_task_id = None

        return {
            "system_instructions": full_instructions,
            "capabilities": capabilities,
            "tools": custom_tools,
            "policies": policies,
            "workspaces": resolved_workspaces,
            "save_dir": str(save_dir),
            "skills_paths": skills_paths,
            "hooks": [on_pre_tool, on_post_tool, on_tool_err],
        }

    def _get_default_discord_approval_handler(self, session_id: Optional[str]) -> Callable[[ToolCall], Any]:
        """Returns the default Discord polling confirmation handler.

        Args:
            session_id: The conversation session identifier.

        Returns:
            The approval handler callable.
        """
        async def my_approval_handler(tool_call: ToolCall) -> bool:
            import uuid
            task_id = str(uuid.uuid4())
            memory.add_active_task(task_id, tool_call.name, str(tool_call.args))
            memory.update_active_task_status(task_id, "pending_approval")
            
            # Post the approval request to Discord
            await memory.ask_discord_approval(task_id, tool_call.name, str(tool_call.args))
            
            # Poll database status until approved or denied
            while True:
                await asyncio.sleep(1.0)
                status = memory.get_active_task_status(task_id)
                if status == "approved":
                    return True
                elif status and status.startswith("denied"):
                    feedback = status.split(":", 1)[1].strip() if ":" in status else ""
                    raise PermissionError(f"Permission denied by user. Feedback: {feedback}" if feedback else "Permission denied by user.")
        return my_approval_handler

    def verify_agent_outputs(self, session_id: Optional[str]) -> Optional[str]:
        """Runs validation and compile checks on workspace code modified during the session.

        Checks modifying files via Git and runs python compilation/json checks.

        Args:
            session_id: The current conversation session ID.

        Returns:
            Error message details if validation fails, otherwise None.
        """
        import subprocess
        try:
            # Get list of modified files in the git workspace
            res = subprocess.run(
                ["git", "diff", "--name-only"],
                capture_output=True,
                text=True,
                check=True
            )
            modified_files = res.stdout.strip().split("\n")
            for filename in modified_files:
                if not filename:
                    continue
                path = Path(os.getcwd()) / filename
                if path.exists() and path.suffix == ".py":
                    import py_compile
                    try:
                        py_compile.compile(str(path), doraise=True)
                    except py_compile.PyCompileError as e:
                        return f"Syntax error in {filename}: {e.msg}"
                elif path.exists() and path.suffix == ".json":
                    import json
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            json.load(f)
                    except Exception as e:
                        return f"JSON syntax error in {filename}: {str(e)}"
        except Exception:
            # Fallback if git is not present/configured or fails
            pass
        return None

    async def get_or_create_agent(
        self,
        model: Optional[str] = None,
        session_id: Optional[str] = None,
        custom_instructions: Optional[str] = None,
        disable_tools: bool = False,
        roleplay: bool = False,
        workspaces: Optional[List[str]] = None,
        auto_approve: bool = False,
        prompt: Optional[str] = None,
        custom_approval_handler: Optional[Callable[[ToolCall], Any]] = None,
        agent_profile: Optional[str] = None
    ) -> Any:
        """Acquires a new or existing agent instance configured via the orchestrator config payload.

        Reuses the active agent instance if the parameters (model, custom instructions, tools option,
        and roleplay flag) remain identical. Otherwise, constructs a new one.

        Args:
            model: Optional LLM model override name.
            session_id: Optional conversation session identifier.
            custom_instructions: Optional overlay system instructions.
            disable_tools: Toggle to disable tool use capabilities.
            roleplay: Toggle to activate roleplay mode.
            workspaces: Folders allowed to interact.
            auto_approve: If True, tool executions are auto-approved.
            prompt: Initial execution query/prompt.
            custom_approval_handler: User confirmation prompt callback.
            agent_profile: Optional specialist profile identifier.

        Returns:
            The configured KeylessAgyAgent instance.
        """
        model_name = model or "gemini-3.5-flash"
        lookup_id = session_id or "default"

        # Check if we can reuse the active agent instance
        session_data = self.active_agents.get(lookup_id)
        if session_data is not None:
            agent = session_data["agent"]
            needs_reconstruct = False
            if model_name != session_data["model"]:
                needs_reconstruct = True
            elif custom_instructions != session_data["instructions"]:
                needs_reconstruct = True
            elif disable_tools != session_data["disable_tools"]:
                needs_reconstruct = True
            elif roleplay != session_data["roleplay"]:
                needs_reconstruct = True

            if needs_reconstruct:
                try:
                    await agent.__aexit__(None, None, None)
                except Exception:
                    pass
                self.active_agents.pop(lookup_id, None)

        if lookup_id not in self.active_agents:
            # 1. Build standard configuration dictionary
            config_args = await self.prepare_agent_config(
                model=model_name,
                session_id=session_id,
                custom_instructions=custom_instructions,
                disable_tools=disable_tools,
                roleplay=roleplay,
                workspaces=workspaces,
                auto_approve=auto_approve,
                prompt=prompt,
                custom_approval_handler=custom_approval_handler
            )

            # 2. Resolve mapped conversation_id for Discord sessions to avoid lock collisions and isolate history
            resolved_conv_id = None
            if session_id:
                if session_id.startswith("discord-"):
                    from agent import memory
                    mem = memory.load_memory()
                    session_mappings = mem.setdefault("key_value", {}).get("session_mappings", {})
                    if not isinstance(session_mappings, dict):
                        session_mappings = {}
                        mem.setdefault("key_value", {})["session_mappings"] = session_mappings
                    
                    if session_id not in session_mappings:
                        import uuid
                        new_uuid = str(uuid.uuid4())
                        session_mappings[session_id] = new_uuid
                        memory.update_key_value("session_mappings", session_mappings)
                        
                        # Store session metadata for listing
                        session_metadata = mem.setdefault("key_value", {}).setdefault("session_metadata", {})
                        from datetime import datetime
                        session_metadata[new_uuid] = {
                            "title": f"[{agent_profile.upper() if agent_profile else 'COORDINATOR'}] Discord Channel",
                            "profile": agent_profile or "coordinator",
                            "created_at": datetime.now().isoformat()
                        }
                        memory.update_key_value("session_metadata", session_metadata)
                        
                    resolved_conv_id = session_mappings[session_id]
                else:
                    resolved_conv_id = session_id

            agent = KeylessAgyAgent(
                model=model_name,
                system_instructions=config_args["system_instructions"],
                conversation_id=resolved_conv_id,
                timeout=600.0,
                roleplay=roleplay
            )
            agent = await agent.__aenter__()

            self.active_agents[lookup_id] = {
                "agent": agent,
                "model": model_name,
                "instructions": custom_instructions,
                "disable_tools": disable_tools,
                "roleplay": roleplay
            }

        return self.active_agents[lookup_id]["agent"]


# Global orchestration service instance
orchestration_service: OrchestrationService = OrchestrationService()
