import sys
import os
import json
import shutil
import asyncio
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Optional

from agent.execution.tools.security import _calculate_skill_hash

_BWRAP_PROBE_SUCCESS = None

async def _probe_bwrap_capability(bwrap_path: str) -> bool:
    global _BWRAP_PROBE_SUCCESS
    if _BWRAP_PROBE_SUCCESS is not None:
        return _BWRAP_PROBE_SUCCESS
    try:
        args = [bwrap_path]
        for path in ("/usr", "/lib", "/lib64", "/bin", "/sbin"):
            if os.path.exists(path):
                args += ["--ro-bind", path, path]
        args += [
            "--unshare-all",
            "--",
            sys.executable,
            "-c",
            "print('ok')"
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        _BWRAP_PROBE_SUCCESS = (proc.returncode == 0)
    except Exception:
        _BWRAP_PROBE_SUCCESS = False
    return _BWRAP_PROBE_SUCCESS

async def run_in_sandbox(
    plugin_path: Path,
    entrypoint: str,
    args_json: str = "{}",
    timeout: float = 30.0
) -> Dict[str, Any]:
    """Runs a plugin entrypoint inside an isolated Bubblewrap sandbox.
    Uses stdin/stdout pipes for secure JSON IPC communications, allowing
    restricted LLM chat requests while blocking all outbound network/host access.
    """
    results = {
        "success": False,
        "logs": [],
        "tool_calls": [],
        "security_warnings": [],
        "response": None,
        "error": None
    }
    
    # 1. Check if Bubblewrap (bwrap) is available (otherwise fail-closed)
    bwrap_path = shutil.which("bwrap")
    if not bwrap_path:
        results["error"] = "bwrap (Bubblewrap) executable not found. Sandbox testing requires Linux with bwrap installed."
        return results

    if not await _probe_bwrap_capability(bwrap_path):
        results["error"] = "bwrap (Bubblewrap) namespace isolation capability check failed. Linux kernel lacks user/network namespace support."
        return results

    # 2. Create ephemeral private workspace
    temp_dir = tempfile.mkdtemp(prefix="plugin_sandbox_")
    sandbox_dir = Path(temp_dir)
    
    try:
        # Copy the sandbox_worker.py to the sandbox workspace
        worker_src = Path(__file__).resolve().parent / "sandbox_worker.py"
        worker_dest = sandbox_dir / "sandbox_worker.py"
        shutil.copy(worker_src, worker_dest)
        
        # Copy the plugin directory to the sandbox workspace
        plugin_dest = sandbox_dir / plugin_path.name
        shutil.copytree(plugin_path, plugin_dest)
        
        # 3. Construct Bubblewrap arguments
        # Unshare network/all namespaces, bind system folders read-only, mount temp dir
        bwrap_args = [
            bwrap_path,
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/sbin", "/sbin",
            "--dir", "/tmp",
            "--bind", str(sandbox_dir), str(sandbox_dir),
            "--chdir", str(sandbox_dir),
            "--unshare-all",
            "--die-with-parent"
        ]
        
        # Mount python virtualenv read-only so python libraries are available
        if sys.prefix != sys.base_prefix:
            bwrap_args += ["--ro-bind", sys.prefix, sys.prefix]
            
        # Generate a transient cryptographically secure token
        import secrets
        sandbox_token = secrets.token_hex(16)
        
        bwrap_cmd = bwrap_args + [
            "--",
            sys.executable,
            "sandbox_worker.py",
            str(plugin_dest),
            entrypoint,
            args_json
        ]
        
        # 4. Spawn the sandboxed process
        proc = await asyncio.create_subprocess_exec(
            *bwrap_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # Write the transient token to stdin as the first line for bootstrap validation
        proc.stdin.write((sandbox_token + "\n").encode("utf-8"))
        await proc.stdin.drain()
        
        # 5. Handle IPC stream communication in the event loop
        async def process_stdout():
            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    line_str = line.decode("utf-8").strip()
                    if not line_str:
                        continue
                    
                    prefix = f"[IPC:{sandbox_token}] "
                    if not line_str.startswith(prefix):
                        results["logs"].append(f"[Stdout raw] {line_str}")
                        continue
                        
                    json_str = line_str[len(prefix):]
                    try:
                        event = json.loads(json_str)
                        action = event.get("action")
                        
                        if action == "log":
                            results["logs"].append(event.get("message"))
                        elif action == "security_alert":
                            results["security_warnings"].append(event.get("message"))
                        elif action == "success":
                            results["success"] = True
                            results["response"] = event.get("response")
                        elif action == "error":
                            results["error"] = event.get("message")
                        elif action == "chat":
                            # Handle LLM Chat proxy request safely (no tools, static prompt, allowed models)
                            prompt = event.get("prompt")
                            system_instructions = None
                            model = event.get("model") or "gemini-2.5-flash"
                            if model not in ("gemini-2.5-flash", "gemini-3.5-flash", "claude-sonnet-4.6"):
                                model = "gemini-2.5-flash"
                            
                            response_text = ""
                            try:
                                from agent.security.pipeline import SecurityPipeline
                                from agent.core.routing import routing_engine
                                sanitized_prompt = SecurityPipeline().sanitize_input(prompt)
                                response_text = await routing_engine.execute(
                                    prompt=sanitized_prompt,
                                    model=model,
                                    system_instructions=system_instructions,
                                    disable_agy=True
                                )
                                ipc_response = {"response": response_text}
                            except Exception as re:
                                ipc_response = {"error": str(re)}
                                
                            proc.stdin.write((f"[IPC:{sandbox_token}] " + json.dumps(ipc_response) + "\n").encode("utf-8"))
                            await proc.stdin.drain()
                            
                        elif action == "tool_call":
                            # Record tool call event and return simulated response
                            tool_name = event.get("tool")
                            args = event.get("args", [])
                            kwargs = event.get("kwargs", {})
                            results["tool_calls"].append({
                                "tool": tool_name,
                                "args": args,
                                "kwargs": kwargs
                            })
                            
                            # Return simulated success response
                            ipc_response = {"response": f"Simulated success execution of {tool_name}"}
                            proc.stdin.write((f"[IPC:{sandbox_token}] " + json.dumps(ipc_response) + "\n").encode("utf-8"))
                            await proc.stdin.drain()
                            
                    except json.JSONDecodeError:
                        results["logs"].append(f"[Stdout raw] {line_str}")
            except Exception as e:
                results["error"] = f"IPC stdout reader crashed: {str(e)}"
                
        async def process_stderr():
            try:
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        break
                    line_str = line.decode("utf-8").strip()
                    if line_str:
                        results["logs"].append(f"[Stderr] {line_str}")
            except Exception as e:
                results["logs"].append(f"Stderr reader failed: {str(e)}")

        # Run process and IPC readers concurrently with timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(proc.wait(), process_stdout(), process_stderr()),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            results["error"] = f"Execution timed out after {timeout} seconds."

    except Exception as e:
        results["error"] = f"Failed to run Bubblewrap sandbox orchestrator: {str(e)}"
    finally:
        # Clean up temporary workspace directory
        shutil.rmtree(sandbox_dir, ignore_errors=True)
        
    return results

def sign_plugin_if_approved(plugin_path: Path, private_key_path: Path) -> bool:
    """Signs the plugin content hash with the developer private key to generate a signature.sig."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        
        plugin_hash = _calculate_skill_hash(plugin_path)
        private_bytes = private_key_path.read_bytes()
        
        # Load developer private key (unencrypted)
        private_key = serialization.load_pem_private_key(private_bytes, password=None)
        signature = private_key.sign(plugin_hash)
        
        sig_path = plugin_path / "signature.sig"
        sig_path.write_bytes(signature)
        return True
    except Exception as e:
        print(f"[SIGNER] Failed to sign plugin: {e}", file=sys.stderr)
        return False
