import sqlite3
import asyncio
import json
import urllib.request
import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Setup paths so we can import from src/agent or run stand-alone
sys.path.append(str(Path(__file__).parent.parent))

from agent import memory
from agent.keyless import KeylessAgyAgent

# DB path resolved from memory module
DB_PATH = memory.DB_FILE_PATH

def get_discord_config():
    config_path = Path(__file__).parent.parent.parent / "discord" / "config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[QUIET-OBSERVER] Error loading Discord config: {e}")
    return {}

def get_bot_token():
    env_path = Path(__file__).parent.parent.parent / "discord" / ".env"
    if env_path.exists():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("DISCORD_BOT_TOKEN="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception as e:
            print(f"[QUIET-OBSERVER] Error loading Discord .env: {e}")
    return None

def send_discord_alert(text):
    token = get_bot_token()
    if not token:
        print("[QUIET-OBSERVER] No Discord bot token found.")
        return False
        
    config = get_discord_config()
    channel_id = 1518056970538586272  # Default control-room fallback
    
    for cid, info in config.get("channels", {}).items():
        if info.get("channel_name") == "control-room":
            try:
                channel_id = int(cid)
                break
            except ValueError:
                pass
                
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com/Rapptz/discord.py 2.3.2) Python/3.10"
    }
    
    # Discord messages are capped at 2000 chars, so truncate safely if needed
    if len(text) > 1950:
        text = text[:1950] + "\n... [truncated]"

    data = json.dumps({"content": text}).encode("utf-8")
    
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.getcode() == 200
    except Exception as e:
        print(f"[QUIET-OBSERVER] Failed to send Discord alert: {e}", file=sys.stderr)
        return False

async def run_quiet_observer(days: int = 1):
    print(f"[QUIET-OBSERVER] Running background pattern analysis (history range: last {days} day(s))...")
    if not DB_PATH.exists():
        print(f"[QUIET-OBSERVER] Database does not exist at {DB_PATH}.")
        return

    conn = sqlite3.connect(DB_PATH)
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
    log_entries = []
    accumulated_len = 0
    max_log_chars = 20000  # Safe boundary to avoid CLI command argument limits

    for step in steps:
        role = step["role"]
        content = step["content"] or ""
        tool_name = step["tool_name"]
        tool_result = step["tool_result"] or ""

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
        model="gemini-3.5-flash",
        system_instructions="You are a passive, silent background observer agent dedicated to learning patterns and process optimization."
    )
    
    response = await agent.chat(prompt)
    output = ""
    async for chunk in response:
        output += chunk

    print(f"[QUIET-OBSERVER] Pattern analysis completed.")
    
    # Save the report to a stable location
    output_dir = Path(__file__).parent.parent.parent / "scratch"
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
    send_discord_alert(discord_alert)

if __name__ == "__main__":
    days_to_check = 1
    if len(sys.argv) > 1:
        try:
            days_to_check = int(sys.argv[1])
        except ValueError:
            pass
    asyncio.run(run_quiet_observer(days_to_check))
