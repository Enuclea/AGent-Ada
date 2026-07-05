import ast
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List
from agent import memory
from agent.storage.db import get_connection
from agent.keyless import KeylessAgyAgent, TaskPriority

# Plugin hook: error data providers
# Plugins call register_error_data_provider(fn) where fn(since_iso: str) -> List[str]
_error_data_providers: List[Callable[[str], List[str]]] = []

def register_error_data_provider(provider_fn: Callable[[str], List[str]]) -> None:
    """Register a plugin function that returns supplementary error log lines.

    The function receives a single argument: an ISO timestamp string representing
    the cutoff date. It should return a list of formatted error log strings.
    """
    if provider_fn not in _error_data_providers:
        _error_data_providers.append(provider_fn)

async def run_meta_evaluation(days: int = 1):
    db_path = memory.DB_FILE_PATH
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get failures in last X days
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    
    # 1. Failed tasks
    cursor.execute(
        "SELECT id, name, details, started_at, status FROM active_tasks WHERE status IN ('failed', 'denied') AND started_at >= ?",
        (since,)
    )
    failed_tasks = cursor.fetchall()
    
    task_log_data = []
    for t in failed_tasks:
        cursor.execute("SELECT timestamp, message FROM task_logs WHERE task_id = ? ORDER BY id ASC", (t["id"],))
        logs = cursor.fetchall()
        log_lines = [f"[{l['timestamp']}] {l['message']}" for l in logs]
        task_log_data.append(
            f"Task: {t['name']} (ID: {t['id']})\n"
            f"Details: {t['details']}\n"
            f"Status: {t['status']}\n"
            f"Logs:\n" + "\n".join(log_lines)
        )

    # 2. Collect supplementary error data from registered plugins
    api_log_data = []
    for provider in _error_data_providers:
        try:
            extra_logs = provider(since)
            if extra_logs:
                api_log_data.extend(extra_logs)
        except Exception:
            pass

    conn.close()

    if not task_log_data and not api_log_data:
        print("[META-EVAL] No failed tasks or API errors in the last 24 hours.")
        return

    # Prepare prompt
    prompt = (
        "You are the Meta-Evaluation Agent. Analyze the following failed tasks and API errors "
        "from the last 24 hours to identify root causes and generate lessons learned, edge cases, and a proactive action plan to prevent them from recurring.\n\n"
        "### FAILED TASKS:\n" + "\n---\n".join(task_log_data) + "\n\n"
        "### FAILED API CALLS:\n" + "\n".join(api_log_data) + "\n\n"
        "Format your response as a raw JSON object with the following structure:\n"
        "{\n"
        '  "facts": ["Fact 1", "Fact 2"], // Up to 3 lessons learned / guidelines for memory\n'
        '  "action_plan": [               // Optional proactive steps to repair or modify custom skills\n'
        "    {\n"
        '      "action_type": "improve_skill", // or "create_skill"\n'
        '      "skill_name": "skill-name",\n'
        '      "description": "Optional skill description",\n'
        '      "instructions": "Complete updated detailed markdown instructions for the skill including code-level bug fixes or logic changes",\n'
        '      "script_content": "Optional python or bash helper script content",\n'
        '      "script_filename": "Optional script file name (e.g., run.py)"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Ensure your response is valid JSON. Return ONLY the raw JSON, no markdown wrappers."
    )

    # Execute via keyless agent
    agent = KeylessAgyAgent(
        model="gemini-3.5-flash",
        response_schema=None,
        task_priority=TaskPriority.SCHEDULED_CRITICAL
    )
    
    print("[META-EVAL] Sending prompt to model...")
    response = await agent.chat(prompt)
    output = ""
    async for chunk in response:
        output += chunk
        
    print(f"[META-EVAL] Model output: {output}")
    
    # Parse output and record facts/execute action plan
    try:
        import json
        from agent import tools
        cleaned = output.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
            
        data = json.loads(cleaned)
        
        if isinstance(data, list):
            facts = data
            action_plan = []
        elif isinstance(data, dict):
            facts = data.get("facts", [])
            action_plan = data.get("action_plan", [])
        else:
            facts = []
            action_plan = []

        # Record facts
        for fact in facts:
            print(f"[META-EVAL] Recording new fact: {fact}")
            memory.add_fact(fact)

        # Execute action plan
        for action in action_plan:
            action_type = action.get("action_type")
            skill_name = action.get("skill_name")
            desc = action.get("description")
            inst = action.get("instructions")
            script = action.get("script_content")
            filename = action.get("script_filename")

            if not skill_name:
                continue

            # Safety check: Validate python script syntax before applying action
            if script and filename and filename.endswith(".py"):
                try:
                    ast.parse(script)
                except Exception as syntax_err:
                    print(f"[META-EVAL] Aborted action for {skill_name}: Script contains syntax errors: {syntax_err}")
                    continue

            if action_type == "improve_skill":
                print(f"[META-EVAL] Executing action plan: improve_skill '{skill_name}'")
                res = tools.improve_agent_skill(
                    skill_name=skill_name,
                    description=desc,
                    instructions=inst,
                    script_content=script,
                    script_filename=filename
                )
                print(f"[META-EVAL] Result: {res}")
            elif action_type == "create_skill":
                print(f"[META-EVAL] Executing action plan: create_skill '{skill_name}'")
                if inst:
                    res = tools.create_agent_skill(
                        skill_name=skill_name,
                        description=desc or "Self-improvement skill from Meta-Evaluation",
                        instructions=inst,
                        script_content=script,
                        script_filename=filename
                    )
                    print(f"[META-EVAL] Result: {res}")
    except Exception as e:
        print(f"[META-EVAL] Error parsing/executing model response: {e}")
