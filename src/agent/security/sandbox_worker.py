import sys
import os
import json
import hmac
import types
import asyncio
from pathlib import Path

# Transient IPC Token and Helper
IPC_TOKEN = None

def send_ipc(action, **kwargs):
    payload = {"action": action, **kwargs}
    if IPC_TOKEN:
        sys.stdout.write(f"[IPC:{IPC_TOKEN}] " + json.dumps(payload) + "\n")
    else:
        sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()

# Defense-in-depth telemetry: Monkeypatches below are NOT a security boundary.
# They exist solely to detect and log unauthorized access attempts from naive code paths.
# The real isolation boundary is Bubblewrap (--unshare-all) in sandbox_test.py.
# Untrusted code can trivially bypass these patches via importlib.reload(), ctypes, or
# capturing original references before the patch. Do not treat these as security controls.
def fail_unauthorized(module_name, func_name):
    def block_call(*args, **kwargs):
        send_ipc(
            "security_alert",
            message=f"Blocked attempt to call {module_name}.{func_name}",
            args=[str(a) for a in args],
            kwargs={k: str(v) for k, v in kwargs.items()}
        )
        raise PermissionError(f"Security Policy: Access to {module_name}.{func_name} is blocked in sandbox.")
    return block_call

# Monkeypatch standard library network/execution modules
try:
    import socket
    socket.socket.connect = fail_unauthorized("socket", "connect")
    socket.socket.connect_ex = fail_unauthorized("socket", "connect_ex")
    socket.socket.bind = fail_unauthorized("socket", "bind")
    socket.getaddrinfo = fail_unauthorized("socket", "getaddrinfo")
    socket.gethostbyname = fail_unauthorized("socket", "gethostbyname")
except Exception:
    pass

try:
    import subprocess
    subprocess.Popen = fail_unauthorized("subprocess", "Popen")
    subprocess.run = fail_unauthorized("subprocess", "run")
    subprocess.call = fail_unauthorized("subprocess", "call")
except Exception:
    pass

# 2. Mock Routing Engine for safe LLM calls via Stdin/Stdout IPC
class MockRoutingEngine:
    async def execute(
        self,
        prompt: str,
        model: str,
        system_instructions = None,
        timeout = None,
        conversation_id = None,
        task_priority = None,
    ) -> str:
        send_ipc(
            "chat",
            prompt=prompt,
            model=model,
            system_instructions=system_instructions
        )
        
        line = sys.stdin.readline()
        if not line:
            raise RuntimeError("Sandbox connection closed by host.")
        
        # Verify bidirectional token authentication (constant-time comparison)
        prefix = f"[IPC:{IPC_TOKEN}] "
        if len(line) < len(prefix) or not hmac.compare_digest(line[:len(prefix)], prefix):
            raise RuntimeError("Sandbox security violation: IPC message untrusted.")
        
        response = json.loads(line[len(prefix):])
        if response.get("error"):
            raise RuntimeError(f"Host returned error: {response['error']}")
        return response.get("response", "")

# 3. Mock Agent Core & Tools packages to redirect calls to host
routing_module = types.ModuleType("agent.core.routing")
routing_module.RoutingEngine = MockRoutingEngine
routing_module.routing_engine = MockRoutingEngine()
sys.modules["agent.core.routing"] = routing_module

class MockTools:
    def __getattr__(self, name):
        def mock_tool(*args, **kwargs):
            send_ipc(
                "tool_call",
                tool=name,
                args=[str(a) for a in args],
                kwargs={k: str(v) for k, v in kwargs.items()}
            )
            
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError("Sandbox connection closed by host.")
            
            # Verify bidirectional token authentication (constant-time comparison)
            prefix = f"[IPC:{IPC_TOKEN}] "
            if len(line) < len(prefix) or not hmac.compare_digest(line[:len(prefix)], prefix):
                raise RuntimeError("Sandbox security violation: IPC message untrusted.")
                
            response = json.loads(line[len(prefix):])
            if response.get("error"):
                raise RuntimeError(f"Host tool execution failed: {response['error']}")
            return response.get("response", None)
        return mock_tool

tools_module = types.ModuleType("agent.execution.tools")
tools_module.tools = MockTools()
sys.modules["agent.execution.tools"] = tools_module

# Also mock system_tools, skills_tools submodules
sys.modules["agent.execution.tools.system_tools"] = tools_module
sys.modules["agent.execution.tools.skills_tools"] = tools_module

# 4. Entrypoint to load and run the dynamic plugin/skill
async def run_plugin(plugin_path_str: str, entrypoint_name: str, args_json: str):
    plugin_path = Path(plugin_path_str)
    sys.path.insert(0, str(plugin_path.parent))
    
    try:
        # Log loading event
        send_ipc("log", message=f"Loading plugin: {plugin_path.name}")
        
        # Load the module
        import importlib
        module = importlib.import_module(plugin_path.name)
        
        # Execute the targeted function/entrypoint
        func = getattr(module, entrypoint_name, None)
        if not func:
            raise AttributeError(f"Entrypoint '{entrypoint_name}' not found in plugin module '{plugin_path.name}'.")
            
        kwargs = json.loads(args_json) if args_json else {}
        
        send_ipc("log", message=f"Executing entrypoint: {entrypoint_name}")
        
        # Check if function is async
        if asyncio.iscoroutinefunction(func):
            result = await func(**kwargs)
        else:
            result = func(**kwargs)
            
        send_ipc("success", response=result)
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        send_ipc("error", message=str(e), trace=error_details)

if __name__ == "__main__":
    token = None
    if "--token" in sys.argv:
        try:
            idx = sys.argv.index("--token")
            token = sys.argv[idx + 1]
            sys.argv.pop(idx)
            sys.argv.pop(idx)
        except (ValueError, IndexError):
            pass
        
    IPC_TOKEN = token

    # Scrub environment of sensitive tokens/keys
    safe_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "HOME": "/app"
    }
    os.environ.clear()
    os.environ.update(safe_env)

    if len(sys.argv) < 3:
        print("Usage: python sandbox_worker.py [--token <token>] <plugin_path> <entrypoint> [args_json]", file=sys.stderr)
        sys.exit(1)
        
    plugin_path = sys.argv[1]
    entrypoint = sys.argv[2]
    args_json = sys.argv[3] if len(sys.argv) > 3 else "{}"
    
    asyncio.run(run_plugin(plugin_path, entrypoint, args_json))
