#!/usr/bin/env python3
import os
import sys
import json
import time
import urllib.request
from pathlib import Path

def get_magica_key():
    key = os.environ.get("MAGICA_API")
    if key:
        return key
    env_path = "/home/dan/.env"
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if line.strip().startswith("MAGICA_API="):
                    return line.split("=")[1].strip()
    return None

def scan_repo(repo_path):
    repo = Path(repo_path)
    if not repo.exists():
        return "Repository path does not exist."
    
    lines = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'venv' and d != '.venv' and d != '__pycache__']
        level = len(Path(root).relative_to(repo).parts)
        indent = '│   ' * level
        lines.append(f"{indent}├── {Path(root).name}/")
        subindent = '│   ' * (level + 1)
        for f in files:
            if not f.startswith('.'):
                lines.append(f"{subindent}└── {f}")
    return "\n".join(lines)

def load_critical_code(repo_path):
    import subprocess
    repo = Path(repo_path)
    critical_files = [
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
        "src/agent/core/scheduler.py"
    ]
    
    code_contents = []
    for rel_path in critical_files:
        file_path = repo / rel_path
        if file_path.exists():
            is_modified = True
            try:
                # Run git diff --quiet origin/main -- <rel_path> to see if it differs from remote main
                res = subprocess.run(
                    ["git", "diff", "--quiet", "origin/main", "--", rel_path],
                    cwd=str(repo),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                if res.returncode == 0:
                    is_modified = False
            except Exception:
                pass

            if is_modified:
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    code_contents.append(f"=== FILE: {rel_path} (MODIFIED - LIVE CODE PROVIDED) ===\n{content}\n")
                except Exception as e:
                    code_contents.append(f"=== FILE: {rel_path} (MODIFIED) ===\nError reading file: {e}\n")
            else:
                github_url = f"https://github.com/Enuclea/AGent-Ada/blob/main/{rel_path}"
                code_contents.append(f"=== FILE: {rel_path} (UNMODIFIED - READ FROM REPO) ===\nLive URL: {github_url}\n")
    return "\n".join(code_contents)

def run_model(api_key, model, prompt, system_prompt="", max_tokens=2048):
    url = f"https://api.magica.com/api/v1/nodes/{model}/run"
    req_data = {
        "input": {
            "prompt": prompt,
            "system_prompt": system_prompt,
            "max_tokens": max_tokens,
            "temperature": 0.2
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

def main():
    api_key = get_magica_key()
    if not api_key:
        print("Error: MAGICA_API key not found.")
        sys.exit(1)
        
    repo_path = "/home/dan/AGent-Ada"
    import subprocess
    
    # 1. Automatically commit changes in the repository
    try:
        print("[Roundtable] Checking for local changes to commit...")
        subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True)
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True
        )
        if status_res.stdout.strip():
            print("[Roundtable] Committing local changes...")
            subprocess.run(
                ["git", "-c", "user.name=Roundtable Bot", "-c", "user.email=roundtable@agent.local", "commit", "-m", "Roundtable automatic commit of latest fixes"],
                cwd=repo_path,
                check=True
            )
            print("[Roundtable] Committed successfully.")
        else:
            print("[Roundtable] No local changes to commit.")
    except Exception as e:
        print(f"[Roundtable] Warning: git auto-commit failed: {e}")

    # 2. Attempt to push to the remote public repository
    try:
        print("[Roundtable] Pushing changes to remote public repository...")
        branch_res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True
        )
        current_branch = branch_res.stdout.strip() or "main"
        subprocess.run(["git", "push", "origin", current_branch], cwd=repo_path, check=True)
        print(f"[Roundtable] Successfully pushed to origin/{current_branch}.")
    except Exception as e:
        print(f"[Roundtable] Warning: git push failed (possibly authentication or already up to date): {e}")
    readme_path = os.path.join(repo_path, "README.md")
    readme_content = ""
    if os.path.exists(readme_path):
        with open(readme_path, "r") as f:
            readme_content = f.read()

    security_path = os.path.join(repo_path, "SECURITY.md")
    security_content = ""
    if os.path.exists(security_path):
        with open(security_path, "r") as f:
            security_content = f.read()
            
    repo_structure = scan_repo(repo_path)
    critical_code = load_critical_code(repo_path)
    
    prompt = f"""We are conducting a security peer-review roundtable for "AGent-Ada", a standalone developer-focused autonomous execution harness built on top of the Google AntiGravity SDK.

The public GitHub repository is: https://github.com/Enuclea/AGent-Ada
Please inspect the repository directly for full file layouts and context.

Here is the directory structure:
{repo_structure}

Here is the README.md:
{readme_content}

Here is the SECURITY.md policy and threat model:
{security_content}

Below is the live code of the critical files. To minimize outbound payload size, we only provide the full code body of files that have local modifications compared to the remote main branch. For unmodified files, please refer to their public GitHub URLs:
{critical_code}

Please provide a technical peer review focusing on:
1. Architecture, modularity, and file layout.
2. The security model (keyless loop, safe repository skill ingestion hooks, sandboxing, Landlock, and ASGI middleware).
   - CRITICAL: If you identify any high or critical security issues, vulnerabilities, or design flaws, you MUST indicate /exactly/ where it was seen. State the precise file path, class/function name, code block, and/or line numbers where the issue exists.
3. Critical recommendations for speed, safety, and capability extension.

Roundtable Session Salt: {os.urandom(16).hex()}
"""
    
    models = ["claude_opus_4_8", "deepseek_v3_2", "grok_4_3"]
    reports_dir = "/home/dan/AGent/reports"
    os.makedirs(reports_dir, exist_ok=True)
    
    for model in models:
        print(f"=== Starting review for model: {model} ===")
        sys_prompt = "You are an elite AI systems architect. Provide direct, highly professional, and technical feedback."
        
        run_id = run_model(api_key, model, prompt, sys_prompt, max_tokens=4000)
        if not run_id:
            print(f"Failed to submit run for {model}. Skipping.")
            continue
            
        print(f"Run ID: {run_id}. Polling status...")
        run_data = poll_run(api_key, run_id)
        
        if run_data.get("status") == "COMPLETED":
            output = run_data.get("output", {}).get("output", "")
            output_file = os.path.join(reports_dir, f"{model}_review.md")
            with open(output_file, "w") as f:
                f.write(output)
            print(f"Success! Saved review to {output_file}")
        else:
            print(f"Model {model} failed to complete review.")
            
        # Enforce a 60-second cooldown between models to prevent rate limit build-up
        print("Cooling down for 60 seconds...")
        time.sleep(60)

if __name__ == "__main__":
    main()
