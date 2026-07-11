#!/usr/bin/env python3
"""
Sequential Roundtable — Security Peer Review via Magica API

Sends all critical source files to three frontier models (Claude Opus, DeepSeek, Grok)
for independent security review. Results are aggregated locally and archived with timestamps.

Usage:
    python3 sequential_roundtable.py
"""
import os
import sys
import json
import time
import urllib.request
import shutil
from datetime import datetime, timezone
from pathlib import Path

REPO_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_URL = "https://github.com/Enuclea/AGent-Ada"
REPORTS_DIR = os.path.join(REPO_PATH, "reports")
ARCHIVE_DIR = os.path.join(REPORTS_DIR, "archive")

CRITICAL_FILES = [
    "src/agent/security/sandbox_worker.py",
    "src/agent/security/sandbox_test.py",
    "src/agent/security/pipeline.py",
    "src/agent/security/ast_safety.py",
    "src/agent/core/landlock.py",
    "src/agent/core/plugins.py",
    "src/agent/execution/tools/security.py",
    "src/agent/execution/tools/skills_tools.py",
    "src/agent/core/keyless.py",
    "src/agent/core/orchestrator.py",
    "src/agent/core/registry.py",
    "src/agent/interfaces/web.py",
    "src/agent/api/router.py",
    "src/agent/api/skills.py",
    "src/agent/api/ollama_clone.py",
    "tests/test_ollama_clone.py",
    "src/agent/core/subagent_manager.py",
    "src/agent/core/scheduler.py",
]

MODELS = ["claude_opus_4_8", "deepseek_v3_2", "grok_4_3"]

SYSTEM_PROMPT = (
    "You are an elite AI systems architect conducting a security code review. "
    "Provide direct, highly professional, and technical feedback. "
    "You MUST cite exact file paths, function names, and line numbers for every finding."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_magica_key():
    key = os.environ.get("MAGICA_API")
    if key:
        return key
    env_path = os.path.join(Path.home(), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if line.strip().startswith("MAGICA_API="):
                    return line.split("=", 1)[1].strip()
    return None


def scan_repo(repo_path):
    repo = Path(repo_path)
    if not repo.exists():
        return "Repository path does not exist."
    lines = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('venv', '.venv', '__pycache__')]
        level = len(Path(root).relative_to(repo).parts)
        indent = '│   ' * level
        lines.append(f"{indent}├── {Path(root).name}/")
        subindent = '│   ' * (level + 1)
        for f in files:
            if not f.startswith('.'):
                lines.append(f"{subindent}└── {f}")
    return "\n".join(lines)


def load_critical_code(repo_path):
    """Always inject full file bodies for every critical file."""
    repo = Path(repo_path)
    code_contents = []
    for rel_path in CRITICAL_FILES:
        file_path = repo / rel_path
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                code_contents.append(f"=== FILE: {rel_path} ===\n{content}\n")
            except Exception as e:
                code_contents.append(f"=== FILE: {rel_path} ===\nError reading file: {e}\n")
        else:
            code_contents.append(f"=== FILE: {rel_path} ===\n[File not found on disk]\n")
    return "\n".join(code_contents)


def build_prompt(repo_structure, readme_content, security_content, critical_code):
    salt = os.urandom(16).hex()
    return f"""We are conducting a security peer-review roundtable for "AGent-Ada", a standalone developer-focused autonomous execution harness built on top of the Google AntiGravity SDK.

Public GitHub repository: {REPO_URL}

Here is the directory structure:
{repo_structure}

Here is the README.md:
{readme_content}

Here is the SECURITY.md policy and threat model:
{security_content}

Below is the FULL live source code of every critical security, routing, and execution file. Review these file bodies directly — do not attempt to fetch external URLs.

{critical_code}

Please provide a technical peer review focusing on:
1. Architecture, modularity, and file layout.
2. The security model (keyless loop, safe repository skill ingestion hooks, sandboxing, Landlock, and ASGI middleware).
   - CRITICAL: If you identify any high or critical security issues, vulnerabilities, or design flaws, you MUST indicate /exactly/ where it was seen. State the precise file path, class/function name, code block, and/or line numbers where the issue exists.
3. Critical recommendations for speed, safety, and capability extension.

Roundtable Session Salt: {salt}
"""


# ---------------------------------------------------------------------------
# Magica API interaction
# ---------------------------------------------------------------------------

def run_model(api_key, model, prompt, system_prompt="", max_tokens=4000):
    url = f"https://api.magica.com/api/v1/nodes/{model}/run"
    req_data = {
        "input": {
            "prompt": prompt,
            "system_prompt": system_prompt,
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
    }
    req = urllib.request.Request(url, data=json.dumps(req_data).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")

    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                res_data = json.loads(res.read().decode("utf-8"))
                return res_data.get("runId")
        except urllib.error.HTTPError as he:
            if he.code == 429:
                sleep_time = (attempt + 1) * 30
                print(f"Rate limited (429) on submit {model}. Retrying in {sleep_time}s...", file=sys.stderr)
                time.sleep(sleep_time)
            else:
                print(f"Error submitting {model}: {he}", file=sys.stderr)
                break
        except Exception as e:
            print(f"Error submitting {model}: {e}", file=sys.stderr)
            break
    return None


def poll_run(api_key, run_id):
    url = f"https://api.magica.com/api/v1/nodes/runs/{run_id}"
    start_time = time.time()
    max_duration = 300
    while True:
        if time.time() - start_time > max_duration:
            print(f"Error: Polling timed out for run {run_id}.", file=sys.stderr)
            return {"status": "FAILED", "error": "Polling timeout"}

        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                run_data = json.loads(res.read().decode("utf-8"))
                status = run_data.get("status")
                if status in ["COMPLETED", "FAILED", "CANCELED"]:
                    return run_data
        except urllib.error.HTTPError as he:
            if he.code == 429:
                print(f"Rate limited (429) during polling for {run_id}. Waiting...", file=sys.stderr)
            else:
                print(f"Error polling run {run_id}: {he}", file=sys.stderr)
        except Exception as e:
            print(f"Error polling run {run_id}: {e}", file=sys.stderr)
        time.sleep(20)


# ---------------------------------------------------------------------------
# Aggregation & Archival
# ---------------------------------------------------------------------------

def aggregate_reports(reports_dir, models):
    """Read each model's review and combine into a single aggregate findings document."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    sections = [f"# Aggregated Security Findings\n\n**Generated:** {timestamp}\n"]

    for model in models:
        report_path = os.path.join(reports_dir, f"{model}_review.md")
        if os.path.exists(report_path):
            with open(report_path, "r", encoding="utf-8") as f:
                content = f.read()
            sections.append(f"\n---\n\n## Model: `{model}`\n\n{content}\n")
        else:
            sections.append(f"\n---\n\n## Model: `{model}`\n\n*No report generated.*\n")

    aggregate = "\n".join(sections)
    out_path = os.path.join(reports_dir, "aggregate_findings.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(aggregate)
    print(f"[Roundtable] Aggregate written to {out_path}")
    return out_path


def archive_round(reports_dir, archive_dir, models):
    """Copy current reports + aggregate into a timestamped archive directory."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    round_dir = os.path.join(archive_dir, f"round_{ts}")
    os.makedirs(round_dir, exist_ok=True)

    files_to_archive = [f"{m}_review.md" for m in models] + ["aggregate_findings.md"]
    for fname in files_to_archive:
        src = os.path.join(reports_dir, fname)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(round_dir, fname))

    print(f"[Roundtable] Archived round to {round_dir}")
    return round_dir


# ---------------------------------------------------------------------------
# Git automation
# ---------------------------------------------------------------------------

def commit_and_push(repo_path):
    """Stage, commit, and push any local changes before scanning."""
    import subprocess

    # Commit
    try:
        print("[Roundtable] Checking for local changes to commit...")
        subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True)
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path, capture_output=True, text=True, check=True,
        )
        if status_res.stdout.strip():
            print("[Roundtable] Committing local changes...")
            subprocess.run(
                ["git", "-c", "user.name=Roundtable Bot",
                 "-c", "user.email=roundtable@agent.local",
                 "commit", "-m", "Roundtable automatic commit of latest fixes"],
                cwd=repo_path, check=True,
            )
            print("[Roundtable] Committed successfully.")
        else:
            print("[Roundtable] No local changes to commit.")
    except Exception as e:
        print(f"[Roundtable] Warning: git auto-commit failed: {e}")

    # Push
    try:
        print("[Roundtable] Pushing to remote...")
        branch_res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, check=True,
        )
        current_branch = branch_res.stdout.strip() or "main"
        subprocess.run(["git", "push", "origin", current_branch], cwd=repo_path, check=True)
        print(f"[Roundtable] Pushed to origin/{current_branch}.")
    except Exception as e:
        print(f"[Roundtable] Warning: git push failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    api_key = get_magica_key()
    if not api_key:
        print("Error: MAGICA_API key not found.")
        sys.exit(1)

    repo_path = REPO_PATH

    # Step 1: Commit and push any pending fixes
    commit_and_push(repo_path)

    # Step 2: Build the review prompt with full file bodies
    readme_content = ""
    readme_path = os.path.join(repo_path, "README.md")
    if os.path.exists(readme_path):
        with open(readme_path, "r") as f:
            readme_content = f.read()

    security_content = ""
    security_path = os.path.join(repo_path, "SECURITY.md")
    if os.path.exists(security_path):
        with open(security_path, "r") as f:
            security_content = f.read()

    repo_structure = scan_repo(repo_path)
    critical_code = load_critical_code(repo_path)
    prompt = build_prompt(repo_structure, readme_content, security_content, critical_code)

    print(f"[Roundtable] Prompt size: {len(prompt):,} characters")

    # Step 3: Run each model sequentially
    os.makedirs(REPORTS_DIR, exist_ok=True)

    for model in MODELS:
        print(f"\n=== Starting review for model: {model} ===")
        run_id = run_model(api_key, model, prompt, SYSTEM_PROMPT, max_tokens=4000)
        if not run_id:
            print(f"Failed to submit run for {model}. Skipping.")
            continue

        print(f"Run ID: {run_id}. Polling status...")
        run_data = poll_run(api_key, run_id)

        if run_data.get("status") == "COMPLETED":
            output = run_data.get("output", {}).get("output", "")
            output_file = os.path.join(REPORTS_DIR, f"{model}_review.md")
            with open(output_file, "w") as f:
                f.write(output)
            print(f"Success! Saved review to {output_file}")
        else:
            print(f"Model {model} failed to complete review.")

        # Cooldown between models to avoid rate limits
        print("Cooling down for 60 seconds...")
        time.sleep(60)

    # Step 4: Aggregate all findings into one document
    aggregate_reports(REPORTS_DIR, MODELS)

    # Step 5: Archive this round
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    archive_round(REPORTS_DIR, ARCHIVE_DIR, MODELS)

    print("\n[Roundtable] Round complete.")


if __name__ == "__main__":
    main()
