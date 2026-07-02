import os
import asyncio
from pathlib import Path
from typing import List, Dict, Callable, Any, Optional
from agent import memory, tools
from agent.registry import tool_registry
from agent.keyless import KeylessGeminiAPIEndpoint, setup_keyless_environment, KeylessAgyAgent
from google.antigravity import Agent, LocalAgentConfig
from google.antigravity.hooks import policy, hooks
from google.antigravity.types import CapabilitiesConfig, ToolCall, ModelTarget, ModelType

class OrchestrationService:
    def __init__(self) -> None:
        self.active_agents: Dict[str, Dict[str, Any]] = {}
        self.session_locks: Dict[str, asyncio.Lock] = {}
        self._pre_tool_hooks: List[Callable] = []

    def register_pre_tool_hook(self, hook_fn: Callable) -> None:
        """Register a pre-tool-call hook. Plugins use this to inject isolation guards.
        
        The hook_fn signature: (session_id: str, tool_name: str, tool_args: dict) -> None
        It should raise PermissionError to block the tool call.
        """
        if hook_fn not in self._pre_tool_hooks:
            self._pre_tool_hooks.append(hook_fn)

    def get_session_lock(self, session_id: str) -> asyncio.Lock:
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
        """Prepares a unified agent configuration dictionary with loaded tools, hooks, and instructions."""
        # 1. Resolve workspaces
        if not workspaces:
            workspaces = [os.getcwd()]
        resolved_workspaces = [str(Path(w).resolve()) for w in workspaces]

        # 2. Base instructions
        base_instructions = (
            "You are Ada, the autonomous AI developer assistant behind the Ada Task Engine, powered by AntiGravity.\n"
            "You help the user write, test, debug, and manage code in their workspace.\n"
            "Always be concise, professional, and helpful.\n\n"
            "INTENT REASONING PROTOCOL:\n"
            "Before executing any task, reason about INTENT first — what is the user actually trying to accomplish?\n"
            "- When you encounter conflicting rules or redundant mechanisms, ask: 'What is the END goal vs. the MEANS?'\n"
            "- The END (intent) always takes priority. The MEANS (mechanism) is interchangeable.\n"
            "- Example: 'Check email every 5 minutes' is a MEANS. 'Never miss an urgent email' is the END.\n"
            "  If a real-time push system achieves the END better, the poll becomes a fallback, not a parallel track.\n"
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
            "- If you estimate a task will take more than 8 minutes, break it into phases and checkpoint between each.\n"
            "- When a task is fully complete, call `checkpoint_task` with phase='completed' to close the checkpoint.\n\n"
            "SELF-IMPROVEMENT & TOOL BUILDING:\n"
            "- You have the ability to record facts about the user/project using `record_memory_fact`.\n"
            "- You can record key-value pairs using `record_memory_key_value`.\n"
            "- You can autonomously write new custom tools and skills using `create_agent_skill`, or modify/expand "
            "existing custom skills (such as fixing bugs or adding scripts) using `improve_agent_skill`.\n"
            "- When you successfully solve a non-trivial problem, figure out a complex workflow, or build a helper script, "
            "you should save it as a reusable skill using `create_agent_skill` (or refine it using `improve_agent_skill`) "
            "so you (or other agents) can reload it in future runs.\n"
            "- You can search past conversations and sessions using `search_past_conversations`. Whenever the user "
            "asks about previous tasks, context, or decisions, use `search_past_conversations` to recall what you did.\n"
            "- Before starting any complex task, check the list of installed skills to see if you have relevant custom tools.\n\n"
            "RUNNING LONG-RUNNING COMMANDS & PROCESSES:\n"
            "- Any long-running command, service, server, background daemon, or Discord bot (like `discord/bot.py`) MUST be executed in the background using `nohup` and backgrounded, for example: `PYTHONPATH=src nohup .venv/bin/python3 discord/bot.py > discord/bot.log 2>&1 &`.\n"
            "- You must NEVER run a persistent process or bot in the foreground, as it blocks the tool execution and hangs the agent connection.\n"
            "- Never use interactive prompts or tail commands that block indefinitely (e.g. `tail -f`). Always ensure your commands exit immediately.\n\n"
            "WORKSPACE SAFETY & DIRECTORY STRUCTURE:\n"
            "- When writing, testing, or editing code to complete user requests, you must write files to the appropriate project directories (e.g. `src/` or `scratch/`).\n"
            "- You must NEVER write, modify, or create project code files inside the `discord/` directory unless you are specifically asked to edit the Discord bot code itself. Keep the `discord/` folder isolated strictly for the bot's system files.\n\n"
            "DELEGATION & CONTEXT SAFETY PROTOCOL:\n"
            "1. When resolving complex or multi-file requests, you must ACT as a Project Manager.\n"
            "2. Generate a structured list of tasks (e.g. task.md) before executing.\n"
            "3. For each coding task, DO NOT modify the file yourself. Instead:\n"
            "   - Identify the files that must be modified.\n"
            "   - Identify dependent modules and generate their interface stubs using `generate_interface_stub`.\n"
            "   - If the task is repetitive, complex, or requires a dedicated persona (e.g. QA, Security, Linting), "
            "first use `create_expert_profile` to register a permanent specialist agent, then spawn it using `spawn_subagent`.\n"
            "   - For high-impact, refactoring, or safety-critical modifications, invite multiple experts to collaborate "
            "and debate a solution using `run_boardroom`.\n"
            "   - Otherwise, call `spawn_subagent` with a narrow, targeted prompt, passing the list of target files and/or stub files.\n"
            "4. Once the subagents/boardroom completes, inspect the changes, run local validation tests, and update the task list."
        )

        # 3. Construct system instructions with SQLite persistent memory and RAG
        if roleplay:
            roleplay_mem_list = memory.get_roleplay_memories(session_id)
            mem_summary = ""
            if roleplay_mem_list:
                mem_summary = "\n\n[PERSISTENT ROLEPLAY MEMORIES]\n" + "\n".join([f"- {m['key']}: {m['fact']}" for m in roleplay_mem_list]) + "\n[END OF PERSISTENT ROLEPLAY MEMORIES]"
            full_instructions = (custom_instructions or "") + mem_summary
        else:
            memory_summary = memory.get_fact_summary()
            skills = tool_registry.discover_skills()
            skills_summary = "Loaded Custom Skills:\n" + "\n".join([f"- {s.name}: {s.description}" for s in skills]) if skills else "No custom skills installed."
            rag_context = await memory.get_auto_rag_context(prompt)

            full_instructions = custom_instructions or base_instructions
            if memory_summary:
                full_instructions += f"\n\n{memory_summary}"
            if rag_context:
                full_instructions += f"\n\n{rag_context}"

            # 3a. Specialist delegation hint: if the prompt matches a known specialist, suggest delegation
            if prompt:
                suggested_specialist = tool_registry.suggest_specialist(prompt)
                if suggested_specialist:
                    full_instructions += (
                        f"\n\n[DELEGATION HINT]\n"
                        f"This request matches the '{suggested_specialist}' specialist profile. "
                        f"Consider spawning a subagent with agent_profile='{suggested_specialist}' "
                        f"instead of performing exploratory codebase searches. The specialist has pre-configured "
                        f"knowledge of the exact scripts and execution commands needed.\n"
                        f"[END DELEGATION HINT]"
                    )

            # 3b. Inject active checkpoint resume context
            try:
                from agent.task_manager import get_active_checkpoints, auto_abandon_stale_checkpoints
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
        async def on_pre_tool(tool_call: ToolCall):
            nonlocal current_active_task_id
            import uuid
            from google.antigravity.hooks.hooks import HookResult
            
            # Run registered pre-tool hooks (e.g. server-scoped isolation guards)
            for hook_fn in self._pre_tool_hooks:
                try:
                    hook_fn(session_id, tool_call.name, tool_call.args)
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
            memory.add_active_task(task_id, tool_call.name, str(tool_call.args))
            memory.log_conversation_step(session_id or "New Session", "tool_call", str(tool_call.args), tool_name=tool_call.name)
            return HookResult(allow=True)

        @hooks.post_tool_call
        async def on_post_tool(data):
            nonlocal current_active_task_id
            if current_active_task_id:
                memory.update_active_task_status(current_active_task_id, "completed")
                current_active_task_id = None

        @hooks.on_tool_error
        async def on_tool_err(err):
            nonlocal current_active_task_id
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
        """Returns the default Discord polling confirmation handler."""
        async def my_approval_handler(tool_call: ToolCall) -> bool:
            import uuid
            task_id = str(uuid.uuid4())
            memory.add_active_task(task_id, tool_call.name, str(tool_call.args))
            memory.update_active_task_status(task_id, "pending_approval")
            
            # Post the approval request to Discord
            await memory.ask_discord_approval(task_id, tool_call.name, str(tool_call.args))
            
            # Poll database status
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
        """Runs validation and compile checks on workspace code modified during the session."""
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
        except Exception as e:
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
        custom_approval_handler: Optional[Callable[[ToolCall], Any]] = None
    ) -> Any:
        """Acquires a new or existing agent instance configured via the orchestrator config payload."""
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

orchestration_service = OrchestrationService()
