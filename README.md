# AGent-Ada - Developer Assistant & Task Execution Harness

AGent-Ada is a modular developer assistant, task engine, and automation harness powered by Gemini, Claude, Grok, and local LLMs (via Ollama). It integrates a Discord bot interface, local/remote worker dispatches, and a dynamic execution routing framework to run assistant workflows.

---

## 🛠️ Core Capabilities

### 1. Dynamic Execution Routing & Failover Engine
AGent-Ada features a modular execution routing engine that decouples model invocation from underlying transports. It dynamically resolves execution pathways based on the model requested and configured route statuses:
*   **AntiGravity CLI (`agy`)**: The default execution route that delegates prompts to the AntiGravity CLI (`agy`), utilizing Gemini developer APIs, Claude, or other pre-configured providers.
*   **Grok Fallback (`grok`)**: Functions as a secondary execution route matching `agy` capabilities. It is configured to run when standard `agy` models fail or are bypassed.
*   **Ollama Route (`ollama`)**: Integrates local Ollama models (e.g., `ollama/gemma4:12b`, `qwen3.5:9b`). It automatically loads target hosts from `config/ollama_hosts.json`.

### 2. Specialist Custom Routes (Bring Your Own Route - BYOR)
The routing architecture is completely modular. You can define custom LLM wrappers, third-party API clients (e.g., OpenAI, Magica), or custom local gateways by placing Python modules into `src/agent/routes/custom/`.

### 3. Persistent SQLite-backed Agent Memory
Maintains local application state and context using SQLite:
*   **Memory Facts**: Records key-value pairings and facts to keep RAG context fresh across agent interactions.
*   **Token Pruning & Context Merging**: Optimizes token consumption by automatically pruning and merging historical conversation logs.

### 4. Remote Worker Compute Broker
Supports worker discovery and dispatching:
*   Queries active remote compute workers (e.g., Darwin host with browser and GPU capabilities).
*   Safely dispatches heavy computational tasks from the hub to remote nodes.

---

## ⚙️ Route Configurations

You can configure the status and execution priority of each route using environment variables in your `.env` or systemd service settings:

| Route | Status Variable | Default Status | Priority Variable | Default Priority |
|---|---|---|---|---|
| **Agy** | `ROUTE_AGY_STATUS` | `primary` | `ROUTE_AGY_PRIORITY` | `10` |
| **Grok** | `ROUTE_GROK_STATUS` | `secondary` | `ROUTE_GROK_PRIORITY` | `20` |
| **Ollama** | `ROUTE_OLLAMA_STATUS`| `secondary` | `ROUTE_OLLAMA_PRIORITY` | `30` |

*Route Statuses:*
*   `primary`: Evaluated first during prompt execution.
*   `secondary`: Used as a fallback sequence if primary routes fail.
*   `urgent_only`: Only executed if the task priority is `INTERACTIVE` or `SCHEDULED_CRITICAL`.
*   `off`: Completely disables the route.

---

## 🔌 Bring Your Own Route (BYOR)

Adding a custom LLM wrapper or gateway is straightforward:

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
        # Implement custom API calls or execution logic here
        return "Custom LLM Response"
```

For a concrete starting point, refer to the template **[src/agent/routes/custom/magica_route.py.example](file:///home/dan/AGent-Ada/src/agent/routes/custom/magica_route.py.example)**.

---

## 🗃️ Database Architecture
AGent-Ada maintains agent state in a SQLite database (`history.db`) containing the following key tables:
*   `memory_facts`: Stores key facts and metadata learned about the developer or environment.
*   `memory_key_value`: Stores key-value parameters.
*   `conversation_logs`: Stores conversation execution logs.

---

## 🚀 Running the System

### 1. Prerequisites
Ensure Python 3.11+ is installed. Set up the virtual environment and install dependencies:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Configure Ollama Hosts
Copy the template configuration and modify the target Ollama hosts:
```bash
cp config/ollama_hosts.json.example config/ollama_hosts.json
```

### 3. Run the Discord Bot
Run the Discord assistant bot in the background:
```bash
PYTHONPATH=src nohup .venv/bin/python3 discord/bot.py > discord/bot.log 2>&1 &
```
