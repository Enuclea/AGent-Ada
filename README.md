# Enuclea - Task & Ticket Automation Service

Enuclea is a background service and automation harness designed to integrate and orchestrate support ticket management (**Atera**), calendar tasks (**Morgen**), and email telemetry (**Gmail**). It leverages AI-driven observations, live device telemetry, and centralized request brokerage to automate support operations.

---

## 🛠️ Core Capabilities

### 1. Context-Aware AI Ticket Recommendations
When a new support ticket is ingested:
*   **Atera Telemetry Query**: The system automatically queries Atera's REST API for the device list associated with the ticket's customer.
*   **Machine Name Matching**: It performs a case-insensitive substring search to match the device (agent) name referenced in the ticket title or description.
*   **Live RMM Telemetry Ingestion**: If matched, it retrieves detailed device specifications (online status, OS details, last reboot, IP addresses, and detailed disk capacity usage for all drives).
*   **Gemini Recommendation Engine**: Injects both the ticket details and the live device telemetry into the Gemini model.
*   **Private Note Placement**: Automatically posts the generated observations and suggestions (technical diagnostic steps and recommendations), along with the gleaned telemetry details, as a **private note (internal comment)** on the Atera ticket.
*   **Morgen Task Synchronizer**: Creates a corresponding task in the Morgen calendar task list with the complete telemetry and analysis context.

### 2. Task & Ticket Scheduling Sync
Synchronizes the scheduling state between the Morgen calendar and Atera:
*   **Public Work Schedule Notes**: When an operator schedules or moves a Morgen task linked to a ticket on their calendar, a public comment is automatically added to the corresponding Atera ticket containing the formatted scheduled date and time (e.g., `This task has been scheduled for work at: 2026-06-22 10:45 UTC.`).
*   **Engineering Group Reassignment**: Moves scheduled tickets out of the Triage queue and reassigns them to the **Engineering** technician group in Atera, updating the ticket status to Pending.

### 3. Centralized API Broker (`APIBroker`)
All outgoing communications to Gmail, Morgen, and Atera are funneled through a thread-safe, centralized API Broker:
*   **Automatic Rate Limiting**: Enforces API bucket limits (600 RPM for Atera, 100 RPM for Morgen, 120 RPM for Gmail) to prevent credentials throttling.
*   **Transient Retry & Exponential Backoff**: Automatically catches network failures, timeouts, and HTTP 429 rate limit statuses, applying exponential sleep backoffs to ensure robust request execution.
*   **GET Cache Manager**: Caches standard GET queries with a customizable TTL to prevent redundant calls and minimize API resource consumption.
*   **API Execution Auditing**: Logs details of every outgoing API call (endpoint, service, method, success, execution duration, caching state, and raw error messages) to a local SQLite table `api_call_logs`.

### 4. IT Automation Rollups & Merging
Consolidates redundant notifications from recurring automation tasks:
*   **Master Rollup Ticket**: When the first IT Automation task ticket of the day arrives, the system registers a daily master rollup tracking record.
*   **Consolidated Ticket Merges**: Subsequent automation feedback tickets for that date are closed and marked as `Merged`. The system appends their logs and details as a private note on the daily master ticket and merges their Morgen task representations, leaving a single actionable ticket and calendar task.

### 5. Availability Monitoring & Auto-Healing
Monitors device availability alerts in Atera:
*   **Periodic Offline Verification**: When a device goes offline, the system tracks it. It verifies availability at configured intervals (default 10 minutes).
*   **Escalation**: If the device remains offline across multiple checks, a support ticket is generated and assigned to Engineering.
*   **Self-Healing**: If the device returns online during the check window, the alert is programmatically archived/deleted, and the support ticket is automatically closed.

### 6. USB Disk Space & Alert Silencing
*   Identifies full disk alerts on USB drives (e.g., drive F on DESKTOP).
*   Registers the drive locally and silences matching disk alerts once a ticket has been created or resolved, preventing alert fatigue.

---

## 📐 Key Design Decisions

### Centralized Brokerage
To handle rate-limiting and unreliable APIs, the system was refactored from direct client requests to a centralized `APIBroker`. This isolates retry policies, rate limit policies, logging, and caching in a single module rather than distributing it across the individual client classes.

### Telemetry Pre-Ingestion for LLM Prompts
Passing raw ticket text to a language model often results in generic suggestions (e.g., "Check if the PC is online"). By querying the RMM agent *before* calling the LLM and passing the live status, disk space, and reboot times, the model is able to formulate precise, actionable recommendations immediately upon ticket ingestion.

### Unified Master Rollups
Filing a ticket for every automated cron task results in visual noise. Standardizing rollups on a per-date basis ensures that daily automation feedback remains consolidated in a single log, streamlining review schedules.

---

## 🔗 Execution Routing & Model Failover Engine

AGent-Ada features a modular execution routing engine that decouples model invocation from underlying transports. It supports core AntiGravity CLI (`agy`), Grok fallback executions, local Ollama integrations, and custom execution pathways.

### Core Architecture
All LLM generation requests are dispatched through `RoutingEngine`, which automatically resolves the available execution routes based on the target model, priorities, and runtime statuses.

*   **Core CLI (`agy`)**: The default execution path that delegates calls to the AntiGravity CLI (`agy`), utilizing Gemini developer APIs, Claude, or other pre-configured providers.
*   **Grok Fallback (`grok`)**: Functions as a secondary execution route matching `agy` capabilities. It is configured to run when standard `agy` models fail or are bypassed.
*   **Ollama (`ollama`)**: Integrates local Ollama LLMs (e.g., `ollama/gemma4:12b`, `qwen3.5:9b`). It automatically loads target hosts from `config/ollama_hosts.json`.

### Route Configurations
Routes are managed dynamically. You can configure the status and execution priority of each route using environment variables in your `.env` or systemd service settings:

| Route | Status Variable | Default Status | Priority Variable | Default Priority |
|---|---|---|---|---|
| **Agy** | `ROUTE_AGY_STATUS` | `primary` | `ROUTE_AGY_PRIORITY` | `10` |
| **Grok** | `ROUTE_GROK_STATUS` | `secondary` | `ROUTE_GROK_PRIORITY` | `20` |
| **Ollama** | `ROUTE_OLLAMA_STATUS`| `secondary` | `ROUTE_OLLAMA_PRIORITY` | `30` |

*Statuses:*
*   `primary`: Evaluated first during prompt execution.
*   `secondary`: Used as a fallback sequence if primary routes fail.
*   `urgent_only`: Only executed if the task priority is `INTERACTIVE` or `SCHEDULED_CRITICAL`.
*   `off`: Completely disables the route.

---

## 🔌 Bring Your Own Route (BYOR)

You can easily inject custom LLM wrappers, third-party API clients (e.g., OpenAI, Magica), or custom local gateways into the harness by dropping a Python module into the `src/agent/routes/custom/` directory.

### How to Add a Custom Route

1. **Create the Route File**: Add a new `.py` file to `src/agent/routes/custom/` (e.g., `custom_openai.py`).
2. **Inherit from `BaseRoute`**: Implement the interface defined in `src/agent/routes/base.py`:

```python
from typing import List, Optional
from agent.routes.base import BaseRoute, RouteStatus

class CustomOpenAIRoute(BaseRoute):
    @property
    def name(self) -> str:
        return "custom_openai"

    @property
    def default_status(self) -> RouteStatus:
        return RouteStatus.SECONDARY

    @property
    def default_priority(self) -> int:
        return 15

    @property
    def supported_models(self) -> List[str]:
        return ["gpt-4o", "gpt-3.5-turbo"]

    async def execute(
        self,
        prompt: str,
        model: str,
        system_instructions: Optional[str] = None,
        timeout: Optional[float] = None,
        conversation_id: Optional[str] = None,
    ) -> Optional[str]:
        # Implement custom execution logic (API calls, subprocess, etc.)
        # Return string response on success, or None on failure.
        ...
```

For a concrete starting point, refer to the template **[src/agent/routes/custom/magica_route.py.example](file:///home/dan/AGent/src/agent/routes/custom/magica_route.py.example)**.

---

## 🗄️ Database Architecture
Enuclea maintains local application state in a SQLite database (`enuclea.db`) containing the following key tables:
*   `morgen_tasks`: Tracks Morgen tasks, statuses, scheduled times, and calendar event IDs.
*   `tracked_atera_items`: Maps Morgen tasks to their associated Atera ticket/alert resources.
*   `availability_alert_checks`: Manages the state and check count for offline devices.
*   `daily_automation_rollups`: Maps dates to the daily master rollup ticket and Morgen task.
*   `api_call_logs`: Audit log for all outgoing API requests.
*   `silenced_alerts` & `device_usb_drives`: Handles USB alert suppression.

---

## 🚀 Running the System
The system runs as a systemd service daemon (`ada.service`).

*   **Restart Daemon**:
    ```bash
    sudo systemctl restart ada.service
    ```
*   **Check Daemon Status**:
    ```bash
    sudo systemctl status ada.service
    ```
*   **View Logs**:
    ```bash
    journalctl -u ada.service -n 50 --no-pager
    ```
