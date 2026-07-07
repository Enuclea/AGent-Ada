import os
import re
import json
import uuid
import asyncio
import shutil
import aiohttp
from pathlib import Path
from typing import List, Optional

from agent.execution.tools.constants import yield_requested, logger
from agent.execution.tools.security import _sandbox_command_if_possible

# We import memory inside the file to avoid circular issues
from agent import memory

async def run_command(command: str) -> str:
    """Runs a shell command in the workspace with a timeout limit of 60 seconds.
    
    Args:
        command: The command to execute in the shell.
    """
    from agent.execution import tools
    if yield_requested.get():
        return "[SYSTEM] Tool execution blocked. A subagent has been spawned in this turn. You MUST immediately stop calling tools, return your progress update, and yield your turn. Do NOT attempt to run any more commands or poll for status."

    # CRITICAL: Always scrub environment variables for all executions to prevent token leakage
    env = dict(os.environ)
    keys_to_scrub = [
        "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", 
        "DISCORD_BOT_TOKEN", "MAGICA_API", "JULES_API", "1MIN_AI_API"
    ]
    extra_keys = os.environ.get("ADDITIONAL_SENSITIVE_KEYS")
    if extra_keys:
        keys_to_scrub.extend([k.strip() for k in extra_keys.split(",") if k.strip()])
        
    for key in keys_to_scrub:
        env.pop(key, None)
        
    # Apply sandboxing wrapping
    try:
        sandboxed_cmd = _sandbox_command_if_possible(command)
    except PermissionError as pe:
        return str(pe)

    print(f"\n💻 Running command: {sandboxed_cmd}")
    import sys
    sys.stdout.flush()

    proc = await asyncio.create_subprocess_shell(
        sandboxed_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        output = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
        return output
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise TimeoutError(f"Command '{command}' timed out after 60 seconds.")

def generate_interface_stub(file_path: str) -> str:
    """Extracts only classes, functions, method signatures, and docstrings from a Python file, discarding function bodies.
    
    Args:
        file_path: Absolute or relative path to the Python script.
    """
    import ast
    try:
        path = Path(file_path)
        if not path.exists():
            return f"Error: File not found: {file_path}"
        
        # If it's not a Python file, return first 50 lines as fallback stub
        if not file_path.endswith(".py"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = [f.readline() for _ in range(50)]
            content = "".join([l for l in lines if l])
            return f"# Non-Python File Interface Summary of {path.name}:\n{content}\n... [truncated]"

        with open(path, "r", encoding="utf-8") as f:
            code = f.read()

        tree = ast.parse(code, filename=file_path)
        lines = []
        
        class StubVisitor(ast.NodeVisitor):
            def __init__(self):
                self.indent = 0
                
            def visit_Module(self, node):
                doc = ast.get_docstring(node)
                if doc:
                    lines.append(f'"""\n{doc}\n"""\n')
                self.generic_visit(node)
                
            def visit_ClassDef(self, node):
                indent_str = "    " * self.indent
                base_names = []
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        base_names.append(base.id)
                    elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
                        base_names.append(f"{base.value.id}.{base.attr}")
                    else:
                        base_names.append("object")
                bases_str = f"({', '.join(base_names)})" if base_names else ""
                lines.append(f"{indent_str}class {node.name}{bases_str}:")
                
                doc = ast.get_docstring(node)
                if doc:
                    lines.append(f'{indent_str}    """\n{indent_str}    {doc}\n{indent_str}    """')
                
                self.indent += 1
                orig_len = len(lines)
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        self.visit(child)
                
                if len(lines) == orig_len:
                    lines.append(f"{indent_str}    pass")
                self.indent -= 1
                lines.append("")
                
            def visit_FunctionDef(self, node):
                self._visit_func(node, is_async=False)
                
            def visit_AsyncFunctionDef(self, node):
                self._visit_func(node, is_async=True)
                
            def _visit_func(self, node, is_async: bool):
                indent_str = "    " * self.indent
                prefix = "async def" if is_async else "def"
                args_list = []
                
                if hasattr(node.args, "posonlyargs"):
                    for arg in node.args.posonlyargs:
                        annotation = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
                        args_list.append(f"{arg.arg}{annotation}")
                    if node.args.posonlyargs:
                        args_list.append("/")
                        
                for arg in node.args.args:
                    annotation = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
                    args_list.append(f"{arg.arg}{annotation}")
                    
                if node.args.vararg:
                    annotation = f": {ast.unparse(node.args.vararg.annotation)}" if node.args.vararg.annotation else ""
                    args_list.append(f"*{node.args.vararg.arg}{annotation}")
                    
                if node.args.kwonlyargs:
                    if not node.args.vararg:
                        args_list.append("*")
                    for arg in node.args.kwonlyargs:
                        annotation = f": {ast.unparse(arg.annotation)}" if arg.annotation else ""
                        args_list.append(f"{arg.arg}{annotation}")
                        
                if node.args.kwarg:
                    annotation = f": {ast.unparse(node.args.kwarg.annotation)}" if node.args.kwarg.annotation else ""
                    args_list.append(f"**{node.args.kwarg.arg}{annotation}")
                    
                returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
                lines.append(f"{indent_str}{prefix} {node.name}({', '.join(args_list)}){returns}:")
                
                doc = ast.get_docstring(node)
                if doc:
                    lines.append(f'{indent_str}    """\n{indent_str}    {doc}\n{indent_str}    """')
                lines.append(f"{indent_str}    ...")
                
        StubVisitor().visit(tree)
        return "\n".join(lines)
    except Exception as e:
        return f"Error generating interface stub: {e}"

def _extract_json_block(text: str) -> Optional[dict]:
    """Helper to robustly extract and parse a JSON object from text, ignoring leading/trailing noise."""
    try:
        start_idx = text.find('{')
        end_idx = text.rfind('}')
        if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
            json_str = text[start_idx:end_idx + 1]
            return json.loads(json_str)
    except Exception:
        pass
    
    try:
        return json.loads(text.strip())
    except Exception:
        return None

async def spawn_subagent(
    prompt: str,
    target_files: Optional[List[str]] = None,
    stub_files: Optional[List[str]] = None,
    agent_profile: Optional[str] = None
) -> str:
    """Spawns a subagent with full tool access to perform a task in the workspace.
    
    The subagent runs through the full Ada Task Engine pipeline with real tools
    (run_command, file edits, etc.) and works directly in the workspace. It waits
    for the subagent to complete and returns its response.
    
    Args:
        prompt: Detailed instructions for the subagent's task.
        target_files: File paths to include as context in the prompt (informational only — the subagent has full workspace access).
        stub_files: File paths whose interfaces should be summarized as context.
        agent_profile: Optional profile name (e.g. 'lacie', 'qa_specialist') to load specialized personality and instructions.
    """
    profile_prefix = f"{agent_profile}-" if agent_profile else "sub-"
    subagent_session = f"subagent-{profile_prefix}{uuid.uuid4().hex[:8]}"
    
    # Print real-time progress update to stdout so it streams to the client
    print(f"\n🚀 Spawning tooled subagent ({agent_profile or 'generic'}) in the background...")
    import sys
    sys.stdout.flush()

    # Log start
    from agent.memory import active_session_id_var
    parent_session_id = active_session_id_var.get() or os.environ.get("ACTIVE_SESSION_ID")
    memory.log_subagent_message(subagent_session, "parent", f"Spawning tooled subagent ({agent_profile or 'generic'}) with prompt: {prompt}", parent_session_id=parent_session_id)
    
    # Build enriched prompt with file context
    base_ws = Path(os.getcwd()).resolve()
    
    def is_safe_relative_path(base_path: Path, rel_str: str) -> bool:
        try:
            resolved = (base_path / rel_str).resolve()
            return base_path == resolved or base_path in resolved.parents
        except Exception:
            return False

    enriched_prompt = prompt
    if target_files:
        safe_targets = [f for f in target_files if is_safe_relative_path(base_ws, f)]
        if safe_targets:
            enriched_prompt += f"\n\n[TARGET FILES]\nThe following files are relevant to this task:\n" + "\n".join([f"- {f}" for f in safe_targets]) + "\n[END TARGET FILES]"
            
    if stub_files:
        stub_context = []
        for rel_path in stub_files:
            if not is_safe_relative_path(base_ws, rel_path):
                continue
            src = (base_ws / rel_path).resolve()
            if src.exists() and src.is_file():
                try:
                    stub_content = await asyncio.to_thread(generate_interface_stub, str(src))
                    stub_context.append(f"### {rel_path}\n```\n{stub_content}\n```")
                except Exception:
                    pass
        if stub_context:
            enriched_prompt += "\n\n[INTERFACE STUBS]\n" + "\n".join(stub_context) + "\n[END INTERFACE STUBS]"
    
    # Build API payload — uses the background spawn endpoint
    payload = {
        "subagent_id": subagent_session,
        "parent_session_id": parent_session_id or "New Session",
        "prompt": enriched_prompt,
        "target_files": target_files,
        "stub_files": stub_files,
        "agent_profile": agent_profile
    }
    
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30.0)) as session:
            async with session.post("http://localhost:8050/api/subagents/spawn", json=payload) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    err_msg = f"[FAILED] Subagent API returned HTTP {resp.status}: {err_text}"
                    memory.log_subagent_message(subagent_session, "subagent", err_msg)
                    return json.dumps({
                        "status": "failed",
                        "summary": err_msg
                    })
                
                resp_json = await resp.json()
                sandbox_dir = resp_json.get("sandbox_dir", "")
                
                # Log success
                memory.log_subagent_message(subagent_session, "subagent", f"[SPAWNED] Subagent successfully spawned in background. Sandbox: {sandbox_dir}")
                
                # Always return immediately — subagent runs in the background.
                # Results are tracked via the activity feed and subagent_messages table.
                yield_requested.set(True)
                return json.dumps({
                    "status": "spawned",
                    "subagent_id": subagent_session,
                    "sandbox_dir": sandbox_dir,
                    "summary": f"Subagent ({agent_profile or 'generic'}) spawned successfully. It is running in the background and will report results to the activity feed."
                })
                
    except Exception as e:
        err_msg = f"[FAILED] Subagent execution failed: {e}"
        memory.log_subagent_message(subagent_session, "subagent", err_msg)
        return json.dumps({
            "status": "failed",
            "summary": err_msg
        })

async def create_expert_profile(
    profile_name: str,
    system_instructions: str,
    supporting_code: Optional[str] = None
) -> str:
    """Creates a new permanent specialist agent profile in the workspace.
    This profile can subsequently be invoked using `spawn_subagent` or in a boardroom.
    
    Args:
        profile_name: A clean identifier for the expert agent (e.g. linter_expert, git_manager).
        system_instructions: Detailed rules, guidelines, and context defining the expert's role.
        supporting_code: Optional Python source code to write to `.agents/agents/<profile_name>/runner.py` to support the expert.
    """
    agent_dir = Path(os.getcwd()) / ".agents" / "agents" / profile_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    
    inst_file = agent_dir / "system_instructions.txt"
    with open(inst_file, "w", encoding="utf-8") as f:
        f.write(system_instructions.strip())
        
    if supporting_code:
        runner_file = agent_dir / "runner.py"
        with open(runner_file, "w", encoding="utf-8") as f:
            f.write(supporting_code.strip())
            
    return f"Expert profile '{profile_name}' successfully created and registered at {agent_dir}."

async def run_boardroom(
    task_description: str,
    expert_profiles: List[str],
    target_files: Optional[List[str]] = None,
    model: str = "gemini-1.5-flash"
) -> str:
    """Executes a multi-agent boardroom debate where multiple experts collaborate, critique,
    and refine a solution to a task.
    
    Args:
        task_description: Detailed summary of the work that needs to be done.
        expert_profiles: Names of registered specialist profiles to invite to the boardroom.
        target_files: Relative paths of files the boardroom experts need to read or modify.
    """
    from agent.keyless import KeylessAgyAgent
    from agent.core.registry import tool_registry
    from agent.memory import active_session_id_var
    
    parent_session_id = active_session_id_var.get() or os.environ.get("ACTIVE_SESSION_ID")
    boardroom_id = str(uuid.uuid4())
    sandbox_dir = Path("/tmp") / f"boardroom_sandbox_{boardroom_id}"
    await asyncio.to_thread(sandbox_dir.mkdir, parents=True, exist_ok=True)
    
    current_workspace = os.getcwd()
    base_ws = Path(current_workspace).resolve()
    dest_base = sandbox_dir.resolve()
    
    def is_safe_relative_path(base_path: Path, rel_str: str) -> bool:
        try:
            resolved = (base_path / rel_str).resolve()
            return base_path == resolved or base_path in resolved.parents
        except Exception:
            return False

    # 1. Setup Sandbox Workspace (copy target files)
    def setup_sandbox_sync():
        if target_files:
            for rel_path in target_files:
                if not is_safe_relative_path(base_ws, rel_path):
                    continue
                src = (base_ws / rel_path).resolve()
                dest = (sandbox_dir / rel_path).resolve()
                try:
                    if not (dest_base == dest or dest_base in dest.parents):
                        continue
                except Exception:
                    continue
                if src.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if src.is_dir():
                        shutil.copytree(src, dest, symlinks=True)
                    else:
                        shutil.copy2(src, dest)
        else:
            # Fallback to copying everything except ignores
            for item in base_ws.iterdir():
                if item.name in (".git", ".venv", "__pycache__", ".agents", ".pytest_cache"):
                    continue
                try:
                    if item.is_dir():
                        shutil.copytree(item, sandbox_dir / item.name, symlinks=True)
                    else:
                        shutil.copy2(item, sandbox_dir / item.name)
                except Exception:
                    pass

    await asyncio.to_thread(setup_sandbox_sync)
                
    current_solution = f"Initial Task Request: {task_description}"
    files_modified = []
    summary_history = []
    consensus_reached = False
    
    max_rounds = 3
    for round_idx in range(1, max_rounds + 1):
        print(f"\n💬 [Boardroom] Starting Round {round_idx} of consensus discussion with members: {', '.join(expert_profiles)}...")
        import sys
        sys.stdout.flush()
        
        round_approvals = 0
        
        async def run_expert(profile):
            specialist_inst = tool_registry.resolve_subagent_profile(profile)
            system_instructions = specialist_inst or f"You are the {profile} specialist agent."
            
            subagent_sys = (
                f"{system_instructions}\n\n"
                "You are participating in a multi-agent Boardroom consensus discussion. Your goal is to review, "
                "critique, correct, or build upon the current solution.\n"
                "CONTRACT: You MUST return your response ONLY as a raw JSON object matching the following structure:\n"
                "{\n"
                '  "approved": true | false,\n'
                '  "critique_or_comments": "Your feedback, suggestions, or comments",\n'
                '  "updated_solution_summary": "Summary of changes you made or proposed",\n'
                '  "files_modified": ["list of modified files relative to workspace if any"]\n'
                "}\n"
                "Do not wrap your response in markdown code blocks. Output ONLY raw JSON."
            )
            
            subagent_id = f"boardroom-{profile}-{uuid.uuid4()}"
            
            prompt = (
                f"Boardroom Round {round_idx}.\n"
                f"Task: {task_description}\n\n"
                f"Current Solution State:\n{current_solution}\n\n"
                f"Please review the workspace files, apply edits if needed, and respond with the JSON contract."
            )
            
            memory.log_subagent_message(subagent_id, "parent", f"Inviting {profile} to Boardroom Round {round_idx} with task: {task_description}", parent_session_id=parent_session_id)
            
            expert_model = model
            profile_lower = profile.lower()
            if "claude" in profile_lower:
                expert_model = "magica/claude-opus-4-8"
            elif "deepseek" in profile_lower:
                expert_model = "magica/deepseek-v3.2"
            elif "grok" in profile_lower:
                expert_model = "magica/grok-4.3"

            agent = KeylessAgyAgent(
                model=expert_model,
                system_instructions=subagent_sys,
                conversation_id=subagent_id,
                cwd=str(sandbox_dir),
                timeout=300.0
            )
            
            try:
                async with agent as sub_conn:
                    response = await sub_conn.chat(prompt)
                    output = ""
                    async for chunk in response:
                        output += chunk
                        
                    memory.log_subagent_message(subagent_id, "subagent", f"[SUCCESS] Boardroom contribution from {profile}: {output}", parent_session_id=parent_session_id)
                    return profile, True, output, None, subagent_id
            except Exception as e:
                memory.log_subagent_message(subagent_id, "subagent", f"[FAILED] Boardroom agent error: {e}", parent_session_id=parent_session_id)
                return profile, False, "", e, subagent_id

        tasks = [run_expert(profile) for profile in expert_profiles]
        round_results = await asyncio.gather(*tasks)
        
        for profile, success, output, err, subagent_id in round_results:
            if not success:
                print(f"❌ [Boardroom] Expert {profile} failed to contribute: {err}")
                import sys
                sys.stdout.flush()
                continue
                
            try:
                res_data = _extract_json_block(output)
                if not res_data:
                    raise ValueError("No valid JSON block found in output.")
                approved = res_data.get("approved", False)
                if approved:
                    round_approvals += 1
                critique = res_data.get("critique_or_comments", "")
                summary = res_data.get("updated_solution_summary", "")
                mod_files = res_data.get("files_modified", [])
                
                print(f"👥 [Boardroom] Expert {profile} contribution analyzed (Approved: {approved})")
                import sys
                sys.stdout.flush()
                
                for f in mod_files:
                    if f not in files_modified:
                        files_modified.append(f)
                        
                current_solution = f"Latest Solution Summary: {summary}\nCritique/Comments from {profile}: {critique}"
                summary_history.append(f"[{profile}] Approved: {approved}. Summary: {summary}")
                
            except Exception as parse_err:
                memory.log_subagent_message(subagent_id, "subagent", f"[FAILED] Failed to parse boardroom contribution JSON: {parse_err}", parent_session_id=parent_session_id)
                
        if round_approvals == len(expert_profiles):
            print(f"[BOARDROOM] Consensus reached at round {round_idx}!")
            consensus_reached = True
            break
            
    if not consensus_reached:
        print(f"[BOARDROOM] Consensus not reached after {max_rounds} rounds. Aborting changes.")
        try:
            await asyncio.to_thread(shutil.rmtree, sandbox_dir)
        except Exception:
            pass
        return json.dumps({
            "status": "failure",
            "boardroom_id": boardroom_id,
            "files_modified": [],
            "summary_of_changes": "Boardroom debate ended without consensus after exceeding max rounds.\n" + "\n".join(summary_history),
            "validation_result": "Boardroom debate exceeded max rounds without consensus."
        })
                
    def apply_changes_back_sync():
        for rel_path in files_modified:
            if not is_safe_relative_path(base_ws, rel_path):
                continue
            sandbox_file = sandbox_dir / rel_path
            workspace_file = Path(current_workspace) / rel_path
            try:
                resolved_wf = workspace_file.resolve()
                if not (base_ws == resolved_wf or base_ws in resolved_wf.parents):
                    continue
            except Exception:
                continue
            if sandbox_file.exists() and sandbox_file.is_file():
                workspace_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sandbox_file, workspace_file)
                print(f"[BOARDROOM] Copied accepted boardroom modifications: {rel_path}")

    await asyncio.to_thread(apply_changes_back_sync)
            
    return json.dumps({
        "status": "success",
        "boardroom_id": boardroom_id,
        "files_modified": files_modified,
        "summary_of_changes": "Boardroom debate complete.\n" + "\n".join(summary_history),
        "validation_result": "Boardroom debate reached termination/consensus."
    })

def get_relevant_tests(changed_files: List[str], workspace_root: Optional[str] = None) -> str:
    """Given a list of changed file paths, returns the most relevant test files to run.
    
    Use this tool BEFORE running tests to avoid running the full test suite on every change.
    Only run the full suite as a final gate before committing.
    
    Args:
        changed_files: List of absolute or relative paths to files that were modified.
        workspace_root: Optional workspace root path. Defaults to current working directory.
    
    Returns:
        JSON with targeted test command and file list.
    """
    root = Path(workspace_root) if workspace_root else Path(os.getcwd())
    tests_dir = root / "tests"
    
    if not tests_dir.exists():
        return json.dumps({"command": "pytest tests/", "reason": "No tests directory found, running all tests."})
    
    test_map = {}
    for test_file in tests_dir.glob("test_*.py"):
        module_name = test_file.stem.replace("test_", "")
        test_map[module_name] = str(test_file.relative_to(root))
    
    relevant_tests = set()
    for changed_file in changed_files:
        changed_path = Path(changed_file)
        stem = changed_path.stem
        
        if stem in test_map:
            relevant_tests.add(test_map[stem])
        
        if changed_path.name.startswith("test_"):
            rel = str(changed_path.relative_to(root)) if changed_path.is_absolute() else str(changed_path)
            relevant_tests.add(rel)
    
    if relevant_tests:
        test_list = sorted(relevant_tests)
        cmd = f"pytest {' '.join(test_list)} -v"
        return json.dumps({
            "command": cmd,
            "test_files": test_list,
            "reason": f"Targeted {len(test_list)} test file(s) based on {len(changed_files)} changed file(s).",
            "note": "Run the full suite (pytest tests/ -v) as a final gate before committing."
        })
    else:
        return json.dumps({
            "command": "pytest tests/ -v",
            "reason": "No targeted test mapping found for changed files. Running full suite.",
            "changed_files": changed_files
        })
