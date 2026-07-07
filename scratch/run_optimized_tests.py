#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path

def get_modified_files():
    # Run git status/diff to find modified files
    res = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    files = []
    for line in res.stdout.splitlines():
        if line.strip():
            # e.g., " M src/agent/interfaces/web.py" -> "src/agent/interfaces/web.py"
            parts = line.strip().split(None, 1)
            if len(parts) > 1:
                files.append(parts[1])
    return files

def map_files_to_tests(modified_files):
    test_files = set()
    
    # Mapping dictionary for known core files
    mapping = {
        "src/agent/interfaces/web.py": "tests/test_web.py",
        "src/agent/core/orchestrator.py": "tests/test_orchestrator.py",
        "src/agent/core/registry.py": "tests/test_plugin_loader.py",
        "src/agent/memory.py": "tests/test_memory.py",
        "src/agent/tools.py": "tests/test_tools.py",
        "src/agent/merge.py": "tests/test_merge.py",
        "src/agent/keyless.py": "tests/test_priority_lock.py",
        "src/agent/core/task_manager.py": "tests/test_checkpoint.py",
        "src/agent/observability/telemetry.py": "tests/test_web.py",
        "src/agent/observability/grace_monitor.py": "tests/test_grace_monitor.py",
    }
    
    for f in modified_files:
        f_path = Path(f)
        # If it's a test file itself, run it
        if f.startswith("tests/") and f.endswith(".py"):
            test_files.add(f)
        elif f in mapping:
            test_files.add(mapping[f])
        else:
            # Fallback: check if there is a test file matching the name
            name = f_path.stem
            possible_test = f"tests/test_{name}.py"
            if Path(possible_test).exists():
                test_files.add(possible_test)
                
    return sorted(list(test_files))

def main():
    import os
    env = os.environ.copy()
    env["ADA_DISABLE_SANDBOX"] = "1"

    if "--all" in sys.argv:
        print("Running all tests...")
        cmd = [".venv/bin/pytest", "tests/", "-x", "--tb=short", "-q"]
        res = subprocess.run(cmd, env=env)
        sys.exit(res.returncode)
        
    modified = get_modified_files()
    if not modified:
        print("No modified files detected. Running core tests...")
        test_files = ["tests/test_web.py", "tests/test_tools.py", "tests/test_memory.py"]
    else:
        test_files = map_files_to_tests(modified)
        if not test_files:
            print("No specific tests mapped to modified files. Running core tests...")
            test_files = ["tests/test_web.py", "tests/test_tools.py", "tests/test_memory.py"]
            
    print(f"Running optimized test suite: {', '.join(test_files)}")
    cmd = [".venv/bin/pytest"] + test_files + ["-x", "--tb=short", "-q"]
    res = subprocess.run(cmd, env=env)
    sys.exit(res.returncode)

if __name__ == "__main__":
    main()
