"""Centralized path safety utilities for verifying directory traversal and symlink safety."""

import os
import logging
from pathlib import Path

logger = logging.getLogger("agent.security")

def is_safe_path(base_dir: Path, path: Path, strict: bool = True) -> bool:
    """Resolves target path and base directory, verifying that the target path
    resides strictly within base_dir, and recursively checking that no symlinks
    in the path resolve outside base_dir.
    """
    try:
        base_path = Path(base_dir).resolve()
        target_path = Path(path).resolve()
        
        # Verify target is within base_path
        if strict:
            if base_path not in target_path.parents:
                logger.warning(f"Path safety check failed (strict): '{target_path}' is not strictly under '{base_path}'")
                return False
        else:
            if base_path != target_path and base_path not in target_path.parents:
                logger.warning(f"Path safety check failed: '{target_path}' is not under '{base_path}'")
                return False
            
        # Walk up from target_path to base_path, verifying that any symlinks resolve inside base_path
        curr = target_path
        while curr != base_path and curr != curr.parent:
            if curr.is_symlink():
                resolved_link = curr.resolve()
                if base_path != resolved_link and base_path not in resolved_link.parents:
                    logger.warning(f"Path safety check failed: symlink '{curr}' resolves to '{resolved_link}' outside '{base_path}'")
                    return False
            curr = curr.parent
        return True
    except Exception as e:
        logger.warning(f"Path safety check encountered error: {e}", exc_info=True)
        return False  # Fail-closed
