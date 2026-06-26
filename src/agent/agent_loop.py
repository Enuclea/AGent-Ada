import asyncio
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

from dotenv import load_dotenv
from google.antigravity import Agent, LocalAgentConfig
from google.antigravity.hooks import policy, hooks
from google.antigravity.types import CapabilitiesConfig, BuiltinTools, ToolCall, ModelTarget, ModelType
from agent.keyless import KeylessGeminiAPIEndpoint, setup_keyless_environment
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.markup import escape

from agent import __version__
from agent import memory
from agent import tools

# Load environment variables from ~/.agent/.env and local .env
load_dotenv(Path.home() / ".agent" / ".env")
load_dotenv()

console = Console()

async def run_agent(
    initial_prompt: Optional[str] = None,
    model: Optional[str] = None,
    workspaces: Optional[List[str]] = None,
    session_id: Optional[str] = None,
    save_dir: Optional[str] = None,
    auto_approve: bool = False,
    text_only: bool = False,
    custom_instructions: Optional[str] = None,
    interactive: bool = True,
) -> None:
    """Orchestrates the agent session and handles the CLI execution modes."""
    import uuid
    from agent.orchestrator import orchestration_service
    
    model = model or "gemini-3.5-flash"
    if not workspaces:
        workspaces = [os.getcwd()]
    resolved_workspaces = [str(Path(w).resolve()) for w in workspaces]

    session_auto_approve = auto_approve
    active_status = None
    tool_calls_this_turn = 0
    
    # Custom approval handler for CLI
    async def my_cli_approval_handler(tool_call: ToolCall) -> bool:
        nonlocal session_auto_approve, active_status, tool_calls_this_turn
        tool_calls_this_turn += 1
        
        # Get active task ID from database
        active_tasks = memory.get_active_tasks()
        task_id = active_tasks[0]["id"] if active_tasks else str(uuid.uuid4())
        
        if session_auto_approve:
            return True

        if text_only:
            memory.update_active_task_status(task_id, "denied")
            return False

        memory.update_active_task_status(task_id, "pending_approval")
        await memory.ask_discord_approval(task_id, tool_call.name, str(tool_call.args))

        if active_status:
            active_status.stop()

        console.print()
        console.print(Panel(
            f"[bold]Tool:[/bold] {escape(tool_call.name)}\n"
            f"[bold]Arguments:[/bold] {escape(str(tool_call.args))}\n\n"
            f"[bold cyan]Approval request posted to Discord control-room channel.[/bold cyan]",
            title="🔔 [bold yellow]Tool Confirmation Required[/bold yellow]",
            border_style="yellow",
            expand=False,
        ))

        async def poll_db():
            while True:
                await asyncio.sleep(1.0)
                status = memory.get_active_task_status(task_id)
                if status == "approved":
                    return "approved"
                elif status and status.startswith("denied"):
                    return status

        choice = None
        choice_source = None
        loop = asyncio.get_event_loop()
        
        try:
            if sys.stdin.isatty():
                db_task = asyncio.create_task(poll_db())
                console_task = loop.run_in_executor(
                    None,
                    lambda: input("Allow execution? [y/N/all/none]: ").strip().lower()
                )
                
                done, pending = await asyncio.wait(
                    [db_task, console_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                for p in pending:
                    p.cancel()
                    
                for d in done:
                    try:
                        res = d.result()
                        if isinstance(res, str):
                            choice_source = "discord"
                            if res == "approved":
                                choice = "y"
                            elif res.startswith("denied"):
                                feedback = res.split(":", 1)[1].strip() if ":" in res else ""
                                raise PermissionError(f"Permission denied by user. Feedback: {feedback}" if feedback else "Permission denied by user.")
                        else:
                            choice_source = "console"
                            choice = res
                    except PermissionError:
                        raise
                    except Exception:
                        choice = "n"
            else:
                res = await poll_db()
                choice_source = "discord"
                if res == "approved":
                    choice = "y"
                elif res.startswith("denied"):
                    feedback = res.split(":", 1)[1].strip() if ":" in res else ""
                    raise PermissionError(f"Permission denied by user. Feedback: {feedback}" if feedback else "Permission denied by user.")
                else:
                    choice = "n"
        except (KeyboardInterrupt, EOFError):
            console.print("\n[bold red]Execution denied due to user interrupt.[/bold red]")
            memory.update_active_task_status(task_id, "denied")
            if active_status:
                active_status.start()
            return False

        if active_status:
            active_status.start()

        if choice in ("y", "yes"):
            if choice_source == "console":
                memory.update_active_task_status(task_id, "approved")
            return True
        elif choice == "all":
            session_auto_approve = True
            console.print("[bold green]Auto-approving all subsequent tools in this session.[/bold green]")
            memory.update_active_task_status(task_id, "approved")
            return True
        else:
            console.print("[bold red]Execution denied.[/bold red]")
            if choice_source == "console":
                memory.update_active_task_status(task_id, "denied")
            return False

    # Get or create agent through OrchestrationService
    agent = await orchestration_service.get_or_create_agent(
        model=model,
        session_id=session_id,
        custom_instructions=custom_instructions,
        disable_tools=False,
        roleplay=False,
        workspaces=resolved_workspaces,
        auto_approve=auto_approve,
        prompt=initial_prompt,
        custom_approval_handler=my_cli_approval_handler
    )

    current_session_id = agent.conversation_id

    try:
        if not text_only:
            print_startup_banner(
                session_id=current_session_id,
                workspace_path=resolved_workspaces[0],
                model_name=model or "gemini-3.5-flash",
                interactive=not initial_prompt and interactive
            )

        async def execute_turn(prompt_text: str) -> str:
            nonlocal active_status, tool_calls_this_turn
            tool_calls_this_turn = 0
            
            memory.log_conversation_step(current_session_id, "user", prompt_text)
            
            response = await agent.chat(prompt_text)
            
            thoughts_str = ""
            if not text_only:
                active_status = console.status("[bold dim]Thinking...[/bold dim]", spinner="dots")
                with active_status:
                    async for thought in response.thoughts:
                        thoughts_str += thought
                active_status = None
                
                if thoughts_str:
                    memory.log_conversation_step(current_session_id, "thought", thoughts_str)

            output_content = ""
            if text_only:
                async for chunk in response:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                sys.stdout.write("\n")
                sys.stdout.flush()
            else:
                console.print("[bold purple]Ada >[/bold purple] ", end="")
                async for chunk in response:
                    console.print(chunk, end="", markup=False)
                    output_content += chunk
                console.print()
                
            if output_content:
                memory.log_conversation_step(current_session_id, "assistant", output_content)
                
            if not text_only and tool_calls_this_turn > 1:
                console.print("\n💡 [dim]Tip: Ask me to compile this workflow into a reusable custom skill by saying: \"Save this as a skill called <name>\"[/dim]")
            
            return output_content

        if initial_prompt:
            if not text_only:
                console.print(f"[bold blue]User >[/bold blue] {escape(initial_prompt)}")
            await execute_turn(initial_prompt)
            if not interactive:
                return

        if interactive:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import WordCompleter
            from prompt_toolkit.history import FileHistory

            commands_list = ["/help", "/memory", "/skills", "/tools", "/exit", "/quit", "/reset", "/search", "/multiline"]
            completer = WordCompleter(commands_list, ignore_case=True)
            
            history_file = Path.home() / ".agent" / "history.txt"
            history_file.parent.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(history_file))
            session = PromptSession(
                history=history,
                completer=completer,
                complete_while_typing=True,
            )
            
            multiline_mode = False
            
            while True:
                try:
                    if multiline_mode:
                        prompt_msg = "User (Multiline - Alt+Enter to submit) > "
                        user_input = await session.prompt_async(prompt_msg, multiline=True)
                    else:
                        prompt_msg = "User > "
                        user_input = await session.prompt_async(prompt_msg)
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[bold yellow]Goodbye![/bold yellow]")
                    break

                cleaned_input = user_input.strip()
                if not cleaned_input:
                    continue

                if cleaned_input.startswith("/"):
                    cmd_parts = cleaned_input.split(maxsplit=1)
                    cmd = cmd_parts[0].lower()
                    
                    if cmd in ("/exit", "/quit"):
                        console.print("[bold yellow]Goodbye![/bold yellow]")
                        break
                    elif cmd == "/help":
                        print_help()
                        continue
                    elif cmd == "/memory":
                        print_memory()
                        continue
                    elif cmd in ("/skills", "/tools"):
                        print_skills()
                        continue
                    elif cmd == "/multiline":
                        multiline_mode = not multiline_mode
                        status = "enabled (Press Alt+Enter to submit)" if multiline_mode else "disabled"
                        console.print(f"[bold yellow]Multiline mode {status}.[/bold yellow]")
                        continue
                    elif cmd == "/search":
                        if len(cmd_parts) < 2:
                            console.print("[bold red]Please specify a search query. E.g. /search black[/bold red]")
                        else:
                            query = cmd_parts[1]
                            results_text = tools.search_past_conversations(query)
                            console.print(Panel(results_text, title=f"FTS Search Results: {query}", expand=False))
                        continue
                    elif cmd == "/reset":
                        console.print("[bold yellow]Resetting session. New session started.[/bold yellow]")
                        console.print("[dim]Please restart the CLI to fully reset the context.[/dim]")
                        continue
                    else:
                        console.print(f"[bold red]Unknown slash command: {escape(cmd)}[/bold red]")
                        continue

                await execute_turn(cleaned_input)

    finally:
        # Exit context cleanly
        lookup_id = session_id or "default"
        session_data = orchestration_service.active_agents.pop(lookup_id, None)
        if session_data:
            try:
                await session_data["agent"].__aexit__(None, None, None)
            except Exception:
                pass

def print_help() -> None:
    help_text = """
[bold]Available Commands:[/bold]
  [bold cyan]/help[/bold cyan]       - Show this help message
  [bold cyan]/memory[/bold cyan]     - Display the current persistent memory contents
  [bold cyan]/skills[/bold cyan]     - Display all learned custom skills and tools (alias: [bold cyan]/tools[/bold cyan])
  [bold cyan]/search <q>[/bold cyan] - Full-text search past sessions and logs
  [bold cyan]/multiline[/bold cyan]  - Toggle multiline input mode (Alt+Enter to submit)
  [bold cyan]/exit[/bold cyan]       - Exit the Ada Task Engine console (alias: [bold cyan]/quit[/bold cyan])
"""
    console.print(Panel(help_text.strip(), title="Ada Task Engine Help", expand=False))

def print_skills() -> None:
    installed = tools.list_installed_skills()
    console.print(Panel(escape(installed), title="Learned Custom Skills & Tools", expand=False))

def print_memory() -> None:
    mem = memory.load_memory()
    facts = mem.get("facts", [])
    kv = mem.get("key_value", {})
    
    lines = []
    if facts:
        lines.append("[bold]Remembered facts/notes:[/bold]")
        for fact in facts:
            lines.append(f"  - {escape(fact)}")
    if kv:
        if lines:
            lines.append("")
        lines.append("[bold]Key-value settings/data:[/bold]")
        for k, v in kv.items():
            lines.append(f"  - [cyan]{escape(k)}[/cyan]: {escape(str(v))}")
            
    content = "\n".join(lines) if lines else "[dim]Memory is currently empty.[/dim]"
    console.print(Panel(content, title="Persistent Memory", expand=False))


def print_startup_banner(
    session_id: str,
    workspace_path: str,
    model_name: str,
    interactive: bool = True
) -> None:
    logo = r"""[bold orchid1]
    ___       ___       ___   
   /\  \     /\  \     /\  \  
  /::\  \   /::\  \   /::\  \ 
 /::\:\__\ /:/\:\__\ /::\:\__\
 \/\::/  / \:\/:/  / \/\::/  /
   /:/  /   \::/  /    /:/  / 
   \/__/     \/__/     \/__/  
      - - -   A D A   T A S K   E N G I N E   - - -[/bold orchid1]
"""
    console.print(logo)
    
    # Load memory to check for user nickname
    mem = memory.load_memory()
    kv = mem.get("key_value", {})
    nickname = kv.get("user_name") or kv.get("nickname") or "Developer"
    
    # Parse custom skills count
    skills = []
    if tools.SKILLS_DIR.exists() and tools.SKILLS_DIR.is_dir():
        for folder in tools.SKILLS_DIR.iterdir():
            if folder.is_dir():
                skill_md = folder / "SKILL.md"
                if skill_md.exists() and skill_md.is_file():
                    try:
                        with open(skill_md, "r", encoding="utf-8") as f:
                            content = f.read()
                        fm = tools._parse_frontmatter(content)
                        name = fm.get("name", folder.name)
                        desc = fm.get("description", "No description.")
                        skills.append((name, desc))
                    except Exception:
                        continue

    # Construct combined status and skills content
    status_lines = [
        f"🤖 [bold]Ada Task Engine v{__version__}[/bold] — Hermes-style AntiGravity Wrapper",
        f"👋 Welcome, [bold cyan]{escape(nickname)}[/bold cyan]!",
        "",
        f"• [bold]Model:[/bold] {escape(model_name)}",
        f"• [bold]Workspace:[/bold] {escape(workspace_path)}",
        f"• [bold]Session ID:[/bold] [dim]{escape(session_id or 'New Session')}[/dim]",
        "",
        f"🧠 [bold]Loaded Custom Skills ({len(skills)})[/bold]",
    ]
    if skills:
        for name, desc in skills:
            status_lines.append(f"  • [cyan]{escape(name)}[/cyan]: {escape(desc)}")
    else:
        status_lines.append("  [dim]No custom skills loaded yet. Teach me a skill to build custom tools![/dim]")
        
    status_content = "\n".join(status_lines)
    
    console.print(Panel(status_content, border_style="blue", expand=False))
    
    if interactive:
        console.print("[dim]Type your message or a slash command (e.g. /help, /memory, /exit).[/dim]\n")
