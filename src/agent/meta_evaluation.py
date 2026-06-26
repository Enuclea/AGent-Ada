import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from agent import memory
from agent.keyless import KeylessAgyAgent, TaskPriority

async def run_meta_evaluation(days: int = 1):
    db_path = memory.DB_FILE_PATH
    conn = sqlite3.connect(db_path)
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

    # 2. Failed api calls (only query if enuclea is available)
    api_log_data = []
    try:
        from enuclea.db import DEFAULT_DB_PATH
        if Path(DEFAULT_DB_PATH).exists():
            conn_en = sqlite3.connect(DEFAULT_DB_PATH)
            conn_en.row_factory = sqlite3.Row
            cursor_en = conn_en.cursor()
            cursor_en.execute(
                "SELECT timestamp, service, endpoint, method, success, duration, error FROM api_call_logs WHERE success = 0 AND timestamp >= ?",
                (since,)
            )
            failed_apis = cursor_en.fetchall()
            for api in failed_apis:
                api_log_data.append(
                    f"[{api['timestamp']}] API Error: {api['method']} {api['service']}/{api['endpoint']} "
                    f"Failed in {api['duration']:.2f}s with error: {api['error']}"
                )
            conn_en.close()
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
