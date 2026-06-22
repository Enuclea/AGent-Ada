# AGent-Ada — Standalone Task Engine & Dashboard

**AGent-Ada** is a standalone, developer-focused autonomous execution harness built on top of the **Google AntiGravity SDK**. It wraps AntiGravity's agentic capabilities into a highly interactive terminal CLI and a visual Web Dashboard, offering a keyless development loop integrated with the system-wide `agy` CLI gateway. 

Originally designed as the core foundation for the **Ada Developer Assistant**, this harness enables the agent to inspect workspaces, manage long-term state, and autonomously author or refine custom developer skills. It is designed with a strict security boundary: rather than auto-loading external code, it exposes safe repository hooks that allow the agent to browse, safety-check/inspect, and install individual skills on demand from external ecosystems like **Hermes** and **OpenClaw**.

---

## 🚀 Core Features
1. **Interactive CLI**: Chat with Ada directly from your terminal. Support for multiline input mode and conversational history.
2. **Web Dashboard**: A visual interface to interact with Ada, view task lists, execution logs, and session history.
3. **Keyless Execution**: Bypasses local API key requirements by executing queries through the system-wide AntiGravity CLI (`agy`). Note that `agy` integration requires specific subscription tiers of Gemini-based accounts (such as Gemini Advanced, Gemini Enterprise, or Workspace Ultra plans).
4. **Self-Improvement & Tool Building**: Autonomous generation and editing of custom skills/tools during active sessions.

---

## 🔌 External Repository Skill Hooks (Hermes & OpenClaw)

By default, the Ada Task Engine ships with **no pre-installed tools** in its active skills directory for safety. However, it includes native ingestion hooks to manage and import skills from existing external installations:
- **Hermes Repository**: scans `~/.hermes/skills/` recursively for `SKILL.md` documents.
- **OpenClaw Repository**: scans `~/.openclaw/extensions/` recursively for Node/TypeScript plugins (detecting `package.json`/`openclaw.plugin.json`).

To ensure untrusted code is never run without verification, Ada does not auto-load these external repositories at startup. Instead, she is equipped with dedicated commands to safely manage them:
1. `list_repository_skills` — Browse all available skills and tools in the external repositories.
2. `view_repository_skill_code` — Retrieve and inspect the source code of any available external skill to perform a safety check before downloading.
3. `install_repository_skill` — Explicitly copy and install the safety-checked skill into the active directory (`~/.agent/skills`), registering it for active use.

---

## 📦 Installation & Setup

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd AGent-Ada
   ```

2. **Set up virtual environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```

3. **Verify AntiGravity CLI Setup**:
   Ensure `agy` is installed on your system. By default, Ada will search for `agy` in your `PATH` or at `~/.local/bin/agy`.

---

## 🛠️ Usage

### Interactive CLI Console
Run the interactive CLI loop to chat with Ada:
```bash
python3 -m agent.cli chat
```
You can use the following slash commands in the console:
- `/help` — Show help message
- `/memory` — Print remembered facts/settings
- `/skills` — Show learned skills/tools
- `/multiline` — Toggle multiline input mode (Press `Alt+Enter` to submit)
- `/exit` — Quit console

### Web Dashboard
Start the FastAPI dashboard web server:
```bash
python3 -m agent.cli ui --port 8050 --host 127.0.0.1
```
Then visit `http://127.0.0.1:8050` in your web browser.

---

## 🧪 Running Tests
Execute the unit test suite using `pytest`:
```bash
pytest tests/
```

