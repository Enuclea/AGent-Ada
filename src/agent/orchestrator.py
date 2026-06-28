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

            # 2. Instantiate connection: We are intentionally using agy and wrapping around it.
            agent = KeylessAgyAgent(
                model=model_name,
                system_instructions=config_args["system_instructions"],
                conversation_id=session_id if session_id and not session_id.startswith("discord-") else None,
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
