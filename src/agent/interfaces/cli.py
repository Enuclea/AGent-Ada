import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.markup import escape

from agent.core import agent_loop

console = Console()

def list_sessions(save_dir: str) -> None:
    """Lists saved session IDs and their last modification time."""
    path = Path(save_dir)
    if not path.exists() or not path.is_dir():
        console.print("[dim]No sessions found.[/dim]")
        return

    # Find files/folders (the harness creates session files or subfolders named after conversation IDs)
    entries = []
    for entry in path.iterdir():
        if entry.name.startswith("."):
            continue
        stat = entry.stat()
        name = entry.name
        if name.endswith(".db"):
            name = name[:-3]
        entries.append((name, stat.st_mtime))

    if not entries:
        console.print("[dim]No sessions found.[/dim]")
        return

    # Sort by modification time, newest first
    entries.sort(key=lambda x: x[1], reverse=True)

    table = Table(title="Recent Ada Task Engine Sessions", expand=False)
    table.add_column("Session ID", style="cyan")
    table.add_column("Last Active", style="green")

    for name, mtime in entries:
        dt = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(name, dt)

    console.print(table)

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ada Task Engine: A Hermes-style CLI wrapper around the Google AntiGravity SDK."
    )
    
    # Positionals / Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Optional subcommands")
    
    # 'list' subcommand
    list_parser = subparsers.add_parser("list", help="List recent sessions")
    list_parser.add_argument(
        "--save-dir",
        type=str,
        default=None,
        help="Directory where sessions are stored",
    )
    
    # 'ui' subcommand
    ui_parser = subparsers.add_parser("ui", help="Start the Web UI Dashboard")
    ui_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to run the UI server on (default: 8000)",
    )
    ui_parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to run the UI server on (default: 127.0.0.1)",
    )
    
    # Global/chat arguments
    parser.add_argument(
        "-q", "--query",
        type=str,
        help="Run a single query, print response with thoughts, and exit.",
    )
    parser.add_argument(
        "-z", "--script",
        type=str,
        help="Pipeline/scripting mode: Run single query, output raw text response only, auto-approve all tools.",
    )
    parser.add_argument(
        "-m", "--model",
        type=str,
        default=None,
        help="The Gemini model identifier (e.g. gemini-3.5-flash, gemini-3.5-pro)",
    )
    parser.add_argument(
        "-w", "--workspace",
        type=str,
        action="append",
        help="Workspace directory the agent can access (can specify multiple times). Defaults to current directory.",
    )
    parser.add_argument(
        "-s", "--session",
        type=str,
        default=None,
        help="Resume a specific session by its conversation ID",
    )
    parser.add_argument(
        "--save-dir",
        type=str,
        default=None,
        help="Directory to save/load conversation history",
    )
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Auto-approve all tool calls (no interactive confirmation prompt)",
    )
    parser.add_argument(
        "-i", "--instructions",
        type=str,
        help="Override default system instructions (string prompt or path to file)",
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Do not start interactive loop after the query completes",
    )
    parser.add_argument(
        "--fork",
        type=str,
        default=None,
        help="Fork from an existing session ID"
    )
    parser.add_argument(
        "--fork-step",
        type=int,
        default=999999,
        help="Number of history steps to copy from parent session when forking"
    )

    args, unknown = parser.parse_known_args()

    # Route list subcommand
    if args.command == "list":
        save_dir = args.save_dir or str(Path.home() / ".agent" / "sessions")
        list_sessions(save_dir)
        return

    # Route ui subcommand
    if args.command == "ui":
        import uvicorn
        import webbrowser
        import threading
        import time
        from agent import memory

        memory.clear_active_tasks()
        host = args.host
        port = args.port
        url = f"http://{host}:{port}"

        console.print(f"[bold green]Starting Ada Task Engine Dashboard on {url}...[/bold green]")

        def open_browser():
            time.sleep(1.0)
            webbrowser.open(url)

        threading.Thread(target=open_browser, daemon=True).start()
        uvicorn.run("agent.web:app", host=host, port=port, log_level="info")
        return

    # Check if there is a positional query (fallback when -q/-z are not used but user supplied text)
    initial_prompt = None
    interactive = not args.no_interactive
    text_only = False
    auto_approve = args.yes

    if args.script:
        initial_prompt = args.script
        interactive = False
        text_only = True
        auto_approve = True  # Scripting mode must not block on user prompts
    elif args.query:
        initial_prompt = args.query
        interactive = False
    elif unknown:
        # If user runs `agent "Hello"`, treat it as initial query
        initial_prompt = " ".join(unknown)

    # Read instructions file if a path is provided
    instructions = None
    if args.instructions:
        inst_path = Path(args.instructions)
        if inst_path.exists() and inst_path.is_file():
            try:
                with open(inst_path, "r", encoding="utf-8") as f:
                    instructions = f.read()
            except OSError as e:
                console.print(f"[bold red]Error reading instructions file: {escape(str(e))}[/bold red]")
                sys.exit(1)
        else:
            instructions = args.instructions

    session_id = args.session
    if args.fork:
        parent_session = args.fork
        if parent_session.endswith(".db"):
            parent_session = parent_session[:-3]
        
        import uuid
        import sqlite3
        from agent import memory
        new_session_id = str(uuid.uuid4())
        
        conn = sqlite3.connect(memory.DB_FILE_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT role, content, tool_name, tool_result, timestamp FROM conversation_steps WHERE session_id = ? ORDER BY id ASC LIMIT ?",
                (parent_session, args.fork_step)
            )
            rows = cursor.fetchall()
            if not rows:
                console.print(f"[bold red]Error: No history found for session {parent_session}.[/bold red]")
                sys.exit(1)
            
            for role, content, tool_name, tool_result, timestamp in rows:
                cursor.execute(
                    "INSERT INTO conversation_steps (session_id, timestamp, role, content, tool_name, tool_result) VALUES (?, ?, ?, ?, ?, ?)",
                    (new_session_id, timestamp, role, content, tool_name, tool_result)
                )
            conn.commit()
            console.print(f"[bold green]Forked session {parent_session} into new session: {new_session_id}[/bold green]")
            session_id = new_session_id
        except Exception as e:
            console.print(f"[bold red]Failed to fork session: {e}[/bold red]")
            sys.exit(1)
        finally:
            conn.close()

    if session_id and session_id.endswith(".db"):
        session_id = session_id[:-3]

    # Run the agent async loop
    try:
        asyncio.run(
            agent_loop.run_agent(
                initial_prompt=initial_prompt,
                model=args.model,
                workspaces=args.workspace,
                session_id=session_id,
                save_dir=args.save_dir,
                auto_approve=auto_approve,
                text_only=text_only,
                custom_instructions=instructions,
                interactive=interactive,
            )
        )
    except Exception as e:
        if text_only:
            print(f"Error: {e}", file=sys.stderr)
        else:
            console.print(f"[bold red]An execution error occurred:[/bold red] {escape(str(e))}")
        sys.exit(1)

if __name__ == "__main__":
    main()
