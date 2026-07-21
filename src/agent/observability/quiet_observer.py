"""Quiet Observer: Passive background process pattern analysis and workflow optimizer.

Analyzes conversation logs to extract optimization suggestions, automation ideas,
and persistent memory candidates.
"""

import sqlite3
import asyncio
import json
import urllib.request
import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Setup paths so we can import from src/agent or run stand-alone
sys.path.append(str(Path(__file__).parent.parent))

from agent import memory
from agent.storage.db import get_connection
from agent.keyless import KeylessAgyAgent, TaskPriority

# DB path resolved from memory module
DB_PATH: Path = memory.DB_FILE_PATH

# Import shared Discord notification utilities
try:
    from agent.notifications import send_discord_alert
except ImportError:
    # Fallback for standalone execution
    def send_discord_alert(text: str, channel_name: str = "control-room") -> bool:
        print(f"[QUIET-OBSERVER] Discord notification not available (standalone mode): {text[:100]}...")
        return False

async def run_quiet_observer(days: int = 1) -> None:
    """Queries recent chat logs, performs pattern analysis, and generates recommendations.

    Args:
        days: Number of recent days of history to include in the analysis.
    """
    print(f"[QUIET-OBSERVER] Running background pattern analysis (history range: last {days} day(s))...")
    if not DB_PATH.exists():
        print(f"[QUIET-OBSERVER] Database does not exist at {DB_PATH}.")
        return

    conn = get_connection(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Query recent conversation steps using parameterized query
    cursor.execute(
        "SELECT session_id, timestamp, role, content, tool_name, tool_result "
        "FROM conversation_steps WHERE timestamp >= ? ORDER BY id DESC",
        (since,)
    )
    steps = cursor.fetchall()

    if not steps:
        print("[QUIET-OBSERVER] No conversation steps in the specified window.")
        conn.close()
        return

    # Process and summarize logs. We process starting from the most recent (descending) 
    # to make sure we keep the newest events if we hit the token/length limit.
    log_entries: List[str] = []
    accumulated_len = 0
    max_log_chars = 20000  # Safe boundary to avoid CLI command argument limits

    for step in steps:
        role: str = step["role"]
        content: str = step["content"] or ""
        tool_name: Optional[str] = step["tool_name"]
        tool_result: str = step["tool_result"] or ""

        # Limit lengths of individual contents to keep context compact and readable
        if len(content) > 150:
            content = content[:150] + "... [truncated]"
        if len(tool_result) > 150:
            tool_result = tool_result[:150] + "... [truncated]"

        entry = f"[{step['timestamp']}] ({step['session_id']}) {role.upper()}: {content}"
        if tool_name:
            entry += f"\n  -> Tool Call: {tool_name}\n  -> Result: {tool_result}"
        
        entry_len = len(entry) + 4
        if accumulated_len + entry_len > max_log_chars:
            # We reached the safe log capacity limit; skip older steps
            print(f"[QUIET-OBSERVER] Log history capped at {len(log_entries)} steps to fit CLI buffer limits.")
            break
            
        log_entries.append(entry)
        accumulated_len += entry_len

    # Reverse back to chronological order
    log_entries.reverse()
    log_history = "\n---\n".join(log_entries)
    conn.close()

    # Formulate analysis prompt
    prompt = (
        "You are Ada's Quiet Observer. You analyze conversation logs, user commands, and tool calls "
        "to discover patterns, workflows, bottlenecks, or opportunities for automation and self-improvement.\n\n"
        "Here are the conversation logs from the past period:\n"
        f"```\n{log_history}\n```\n\n"
        "Please analyze this data and formulate a response containing:\n"
        "1. Observed Patterns: Recurring user request topics, repetitive commands, or repeated tool usage.\n"
        "2. Friction Points: Errors encountered, multiple failed command attempts, or long/inefficient multi-step tasks.\n"
        "3. Actionable Automation Ideas: Specifically, what custom skills or scripts could be created or improved to automate these tasks?\n"
        "4. Suggested Memory Facts: List up to 3 succinct sentences of new context, guidelines, or preferences discovered about the user/workspace during this period.\n\n"
        "Format your output as a clean, professional markdown report. Do not include markdown code block wrappers around the whole report."
    )

    print("[QUIET-OBSERVER] Sending conversation history to KeylessAgyAgent for analysis...")
    agent = KeylessAgyAgent(
        model="gemini-3.6-flash",
        system_instructions="You are a passive, silent background observer agent dedicated to learning patterns and process optimization.",
        task_priority=TaskPriority.BACKGROUND
    )
    
    response = await agent.chat(prompt)
    output = ""
    async for chunk in response:
        output += chunk

    print(f"[QUIET-OBSERVER] Pattern analysis completed.")
    
    # Save the report to a stable location
    output_dir = Path(__file__).resolve().parent.parent.parent.parent / "scratch"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_file = output_dir / "process_suggestions.md"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"[QUIET-OBSERVER] Saved suggestions report to {report_file}")

    # Generate a condensed message for Discord
    discord_prompt = (
        "Based on the following process suggestions report, write a short Discord notification summarizing the key patterns and 1-2 top automation recommendations. "
        "Keep the message concise, professional, and under 1200 characters, suitable for posting to a Discord admin channel.\n\n"
        f"Report:\n{output}"
    )
    
    response_discord = await agent.chat(discord_prompt)
    discord_msg = ""
    async for chunk in response_discord:
        discord_msg += chunk
        
    discord_alert = (
        f"🔍 **Ada\'s Quiet Observer: Pattern & Process Report**\n\n"
        f"{discord_msg.strip()}\n\n"
        f"_Detailed report saved to `scratch/process_suggestions.md`._"
    )
    
    print("[QUIET-OBSERVER] Sending Discord alert...")
    await asyncio.to_thread(send_discord_alert, discord_alert)

if __name__ == "__main__":
    days_to_check = 1
    if len(sys.argv) > 1:
        try:
            days_to_check = int(sys.argv[1])
        except ValueError:
            pass
    asyncio.run(run_quiet_observer(days_to_check))
