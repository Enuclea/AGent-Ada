# Ada's Quiet Observer: Pattern Analysis & Optimization Report

This report analyzes conversation logs, user commands, and tool executions from the past period to identify recurring patterns, friction points, and opportunities for automation and workflow optimization.

---

## 1. Observed Patterns

* **High-Frequency Scheduled Tasks:** Two specific scheduled tasks are triggered concurrently every 5 minutes:
  * **Gmail Email Check:** Parses incoming emails for importance and creates Morgen tasks. It executes either via `sync_gmail_emails` or by running the script [run_gmail_sync.py](file:///home/dan/AGent/scratch/run_gmail_sync.py) as an asynchronous background task.
  * **Grace Timekeeper:** Runs health checks on background tasks by executing the script [grace_monitor.py](file:///home/dan/AGent-Ada/src/agent/grace_monitor.py) (also referred to as [grace_monitor.py](file:///home/dan/AGent/src/agent/grace_monitor.py)) directly or by invoking a subagent named Grace.
* **Repetitive Exploratory Tool Usage:** Due to sessions starting fresh ("New Session") for each scheduled execution, agents repeatedly execute search and read commands (e.g., `find_file` for `*grace_monitor.py` or `*gmail*`, followed by `view_file` on those files) to re-verify the codebase layout before actually running the scripts.
* **Redundant Background Execution:** Scripts like [run_gmail_sync.py](file:///home/dan/AGent/scratch/run_gmail_sync.py) are repeatedly launched as background tasks (e.g., `task-30`, `task-41`, `task-50`, `task-83`, `task-91`, `task-99`) every 5 minutes.

---

## 2. Friction Points & Bottlenecks

* **Context Fragmentation & Overhead:** Because each scheduled run triggers a new session, the agent begins with no memory of the file locations. The initial steps are wasted on locating and viewing files that have not changed since the check 5 minutes prior. This introduces latency and wastes token usage.
* **Lack of Asynchronous Follow-Up:** As pointed out by the user (*"You never presented the summary -- you didn't follow u p."*), agents start background tasks and end their turn without waiting for them to complete or presenting their results to the user. This leaves the user in the dark regarding the success or status of the sync operations.
* **Redundant Agent Invocation:** Spawning the `self` subagent (Grace) to run a single monitoring script ([grace_monitor.py](file:///home/dan/AGent-Ada/src/agent/grace_monitor.py)) introduces extra startup delay and resource overhead compared to running the script directly.

---

## 3. Actionable Automation Ideas

* **Establish Project-Scoped Rules (`AGENTS.md`):**
  Create or update a project rule file in the customizations directory to instruct agents on exact locations and command execution syntax for scheduled tasks.
  * *Example Rule:* *"When asked to run the Grace Timekeeper check, bypass file searches and directly execute `.venv/bin/python3 src/agent/grace_monitor.py`. When running the Gmail email check, run `scratch/run_gmail_sync.py` directly."*
* **Build a Dedicated Scheduled Task Runner Skill:**
  Develop a custom skill or wrapper script that combines the logic of [run_gmail_sync.py](file:///home/dan/AGent/scratch/run_gmail_sync.py) and [grace_monitor.py](file:///home/dan/AGent-Ada/src/agent/grace_monitor.py). This script could log health metrics, sync tasks, and write a structured markdown summary to a persistent dashboard file.
* **Synchronous Wait & Report Pattern:**
  Modify the agent's scheduled task instructions to require waiting for background tasks (using `WaitMsBeforeAsync` or verifying the task status) so that the final assistant turn of the session includes a concise summary of the synced emails and system health status.

---

## 4. Suggested Memory Facts

1. The workspace runs two high-frequency scheduled tasks every 5 minutes: a Gmail/Morgen task sync via [run_gmail_sync.py](file:///home/dan/AGent/scratch/run_gmail_sync.py) and a Grace Timekeeper health check via [grace_monitor.py](file:///home/dan/AGent-Ada/src/agent/grace_monitor.py).
2. The user expects the agent to explicitly follow up on background synchronization runs and present a final status summary rather than ending the turn immediately after spawning a background task.
3. The user has access to Claude models via the `agy` CLI and expects Ada to support comparable capabilities to the Hermes agent running on the same host.
