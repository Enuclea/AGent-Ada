"""Centralized constants and shared system protocols for the Ada Task Engine."""

COMMON_PROTOCOL = (
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
