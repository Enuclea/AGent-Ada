import os
import shutil
from pathlib import Path
from typing import Optional, List

import logging
from agent.tools import generate_interface_stub

def is_safe_relative_path(base_path: Path, rel_str: str) -> bool:
    """Security verification helper for subagent workspaces."""
    logger = logging.getLogger("agent.security")
    try:
        # Enforce strict path normalization first
        normalized_rel = os.path.normpath(rel_str)
        if normalized_rel.startswith("../") or normalized_rel == "..":
            logger.warning(f"Path safety check failed: relative path traversal attempt '{rel_str}'")
            return False
            
        base_resolved = base_path.resolve()
        resolved = (base_resolved / normalized_rel).resolve()
        if not (base_resolved == resolved or base_resolved in resolved.parents):
            logger.warning(f"Path safety check failed: '{resolved}' is not under '{base_resolved}'")
            return False
            
        # Walk up from resolved to base_resolved, verifying that any symlinks resolve inside base_resolved
        curr = resolved
        while curr != base_resolved and curr != curr.parent:
            if curr.is_symlink():
                resolved_link = curr.resolve()
                if not (base_resolved == resolved_link or base_resolved in resolved_link.parents):
                    logger.warning(f"Path safety check failed: symlink '{curr}' resolves to '{resolved_link}' outside '{base_resolved}'")
                    return False
            curr = curr.parent
        return True
    except Exception as e:
        logger.warning(f"Path safety check encountered error processing '{rel_str}': {e}", exc_info=True)
        return False  # Fail-closed

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
                        shutil.copytree(src, dest, symlinks=True)
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
                    shutil.copytree(item, sandbox_dir / item.name, symlinks=True)
                else:
                    shutil.copy2(item, sandbox_dir / item.name)
            except Exception:
                pass
