import os
import shutil
from pathlib import Path
from typing import Optional, List

import logging
from agent.tools import generate_interface_stub

def is_safe_relative_path(base_path: Path, rel_str: str) -> bool:
    """Security verification helper for subagent workspaces."""
    try:
        # Enforce strict path normalization first
        rel_str = rel_str.replace('\\', '/')
        normalized_rel = os.path.normpath(rel_str)
        if normalized_rel.startswith("../") or normalized_rel == "..":
            return False
            
        target = base_path / normalized_rel
        from agent.security.path_utils import is_safe_path
        return is_safe_path(base_path, target, strict=True)
    except Exception:
        return False

# Active subagents registry
# Maps subagent_id -> {"task": asyncio.Task, "parent_session_id": str, "agent": KeylessAgyAgent, "response": KeylessAgyResponse}
active_subagents = {}

def setup_sandbox_sync(
    current_workspace: str,
    sandbox_dir: Path,
    target_files: Optional[List[str]],
    stub_files: Optional[List[str]]
):
    base_ws = Path(current_workspace).resolve()
    dest_base = sandbox_dir.resolve()

    # 1. Clone target files/directories if specified
    if target_files:
        for rel_path in target_files:
            if not is_safe_relative_path(base_ws, rel_path):
                continue
            src = (base_ws / rel_path).resolve()
            dest = (sandbox_dir / rel_path).resolve()
            # Verify destination is within sandbox boundary
            try:
                if not (dest_base == dest or dest_base in dest.parents):
                    continue
            except Exception:
                continue
            if src.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if src.is_dir():
                        shutil.copytree(src, dest, symlinks=False)
                    else:
                        shutil.copy2(src, dest)
                except Exception:
                    pass
                    
    # 2. Generate and copy stubs if specified
    if req_stub_files := stub_files:
        for rel_path in req_stub_files:
            if not is_safe_relative_path(base_ws, rel_path):
                continue
            src = (base_ws / rel_path).resolve()
            dest = (sandbox_dir / rel_path).resolve()
            # Verify destination is within sandbox boundary
            try:
                if not (dest_base == dest or dest_base in dest.parents):
                    continue
            except Exception:
                continue
            if src.exists() and src.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    stub_content = generate_interface_stub(str(src))
                    with open(dest, "w", encoding="utf-8") as f:
                        f.write(stub_content)
                except Exception:
                    pass
                    
    # 3. Fallback: If neither target_files nor stub_files are specified, copy entire workspace (backward compatibility)
    if not target_files and not stub_files:
        for item in base_ws.iterdir():
            if item.name in (".git", ".venv", "__pycache__", ".agents", ".pytest_cache"):
                continue
            try:
                if item.is_dir():
                    shutil.copytree(item, sandbox_dir / item.name, symlinks=False)
                else:
                    shutil.copy2(item, sandbox_dir / item.name)
            except Exception:
                pass
