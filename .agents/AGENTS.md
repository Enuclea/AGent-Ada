# Workspace Custom Rules: Specialist Agents & Async Command Tracking

These guidelines are active for all sessions in this workspace.

---

## 1. Specialist Agents for Recurring Tasks
* When running, configuring, or delegating recurring background processes, always call the corresponding specialist agent profile instead of a generic subagent or performing exploratory file searches.
* Supported specialist profiles in `spawn_subagent` (parameter `agent_profile`):
  * `grace_timekeeper`: Executes the system task health monitor (`src/agent/grace_monitor.py`).
  * `gmail_sync`: Performs Gmail email check and Morgen task synchronization (`scratch/run_gmail_sync.py`). Note: This check must run strictly on a 5-minute polling interval to capture urgent time-sensitive emails (e.g., Yelp, Thumbtack, client alerts); do not suggest disabling or extending this interval.
  * `quiet_observer`: Analyzes conversation logs for patterns and opportunities (`src/agent/quiet_observer.py`).
  * `meta_evaluator`: Processes post-mortem evaluations of system errors (`src/agent/meta_evaluation.py`).
  * `stock_trader`: Handles stock portfolio checks and rebalancing (`stock_game/strategy.py`).
  * `solar_monitor`: Reads real-time solar generation, grid power, and battery metrics (`/home/dan/AGent/solar/snapshot.py`).
* **Protocol:** Avoid calling `find_file` or exploratory search tools on cold starts. Directly spawn the corresponding specialist subagent with the exact profile name. When checking solar metrics, the subagent must execute `/home/dan/AGent/solar/.venv/bin/python3 /home/dan/AGent/solar/snapshot.py` directly without searching the codebase or inspecting the discord bot.

---

## 2. Asynchronous Command & Subagent Tracking
* When starting a long-running background task via `run_command` or spawning a subagent via `spawn_subagent`, you must guarantee reliability by setting up a background check-in timer.
* **Protocol:**
  1. Immediately after launching the task/subagent, call the `schedule` tool to set a one-shot check-in timer (e.g., `DurationSeconds=120` or `300`).
  2. Set the `TimerCondition` to the returned `task_id` or `subagent_id`. This ensures the timer cancels automatically if the task completes successfully early.
  3. Set a descriptive `Prompt` like `"Check on the status of background task <task_id>."`
  4. If the timer fires (meaning the process failed, hung, or hit a timeout), use the wakeup notification to check the task status via `manage_task` / `get_subagent_messages` and report the status and error logs back to the user.

---

## 3. Strict Control & Exposure of Atera Skill
* Outside of the designated Discord server (and the administrator's controlled channels), the Atera integration and related features must never be presented, advertised, or made available in any shape or form. It is a tightly controlled administrative utility.
* **Delta Rule:** Within dedicated client spaces/channels on this Discord server, authenticated clients are permitted to query ticket statuses or log new support tickets pertaining specifically to their mapped business/organization.

---

## 4. Subagent Delegation Protocols (Orchestration Precision)
* Whenever spawning a subagent (via `spawn_subagent` or a sandbox), you must act as the primary Orchestrator. 
* You are forbidden from leaving subagents to explore the codebase, perform broad directory scans, or guess tool configurations.
* **Protocol:** You must explicitly inject exact file names, absolute paths, environment settings, execution commands, and expected verification criteria into the subagent's prompt. If a subagent has to run search tools to locate basic tools/code, it constitutes a failure of prompt preparation.

---

## 5. Proactive Timeout Prevention & Course Adjustment
* **Non-Blocking Turns:** When executing tasks (such as running commands or polling subagents), do not block your turn for more than 30-40 seconds (avoid high `WaitMsBeforeAsync` values or synchronous loop polling).
* **No Blocking Coordination Scripts:** Never write custom Python or Bash scripts that block or loop to poll for subagents or background tasks to complete (e.g. using `while True` or `sleep` in a script run via `run_command`). If a task requires multiple subagents to run sequentially (such as Lacie spawning engineers, followed by Val verifying), always leverage the built-in plan step system. Create separate steps in your plan, spawn the subagent for the active step, schedule a check-in timer, and yield control. The system's background scheduler will automatically resume the session when the subagent completes.
* **Yield & Check-In Protocol:** If a process or subagent is taking too long to finish, yield your turn immediately: output a clear progress update, call `schedule` to set a check-in timer, and end your turn. This ensures the web UI input field remains responsive and doesn't hit a proxy timeout.
* **Waking Up:** When resuming from a check-in timer, inspect the task or subagent status via `manage_task` or `get_subagent_messages`. If it is still running, check the latest log progress to verify it is not hung. Decide whether to wait longer (yield again) or abort/restart the process if it has stalled.
* **Optimized Test Execution:** During development and verification cycles, avoid running the entire test suite. Always use the optimized test runner (`python3 scratch/run_optimized_tests.py`) which maps git-modified files to relevant test cases, finishing in seconds instead of minutes. Use the `--all` flag only when final verification is required.

---

## 6. Docker Workspace Access & Direct Host Filesystem Mapping
* Both the private workspace `AGent` (`/home/dan/AGent`) and the public workspace `AGent-Ada` (`/home/dan/AGent-Ada`) are volume-mounted directly inside the Docker containers at identical host paths.
* **Protocol:** Containerized agents (including Ada, Lacie, and any spawned subagents) have direct read and write access to these workspace directories. You must use standard file tools (`view_file`, `replace_file_content`, etc.) directly on these paths rather than writing custom python files or executing command gateways.




