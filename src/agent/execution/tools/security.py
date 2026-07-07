import os
import sys
import shutil
import shlex
from pathlib import Path

def _is_safe_path(base_dir, path) -> bool:
    """Helper that resolves absolute paths and verifies that target path resides strictly within base_dir,
    recursively checking that no symlinks in the path resolve outside base_dir.
    """
    try:
        base_path = Path(base_dir).resolve()
        target_path = Path(path).resolve()
        # Verify target is strictly within base_path
        if base_path not in target_path.parents:
            return False
            
        # Walk up from target_path to base_path, verifying that any symlinks resolve inside base_path
        curr = target_path
        while curr != base_path and curr != curr.parent:
            if curr.is_symlink():
                resolved_link = curr.resolve()
                if not (base_path == resolved_link or base_path in resolved_link.parents):
                    return False
            curr = curr.parent
        return True
    except Exception:
        return False

def _calculate_skill_hash(src_folder: Path) -> bytes:
    import hashlib
    hasher = hashlib.sha256()
    file_paths = []
    for root, dirs, files in os.walk(src_folder):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if f != "signature.sig" and not f.startswith('.'):
                file_paths.append(Path(root) / f)
                
    file_paths.sort(key=lambda p: p.relative_to(src_folder))
    
    for p in file_paths:
        hasher.update(str(p.relative_to(src_folder)).encode('utf-8'))
        try:
            with open(p, "rb") as f:
                while chunk := f.read(8192):
                    hasher.update(chunk)
        except Exception:
            pass
            
    return hasher.digest()

def _verify_skill_signature(src_folder: Path) -> bool:
    sig_path = src_folder / "signature.sig"
    if not sig_path.exists():
        return False
        
    try:
        with open(sig_path, "rb") as f:
            signature = f.read()
            
        skill_hash = _calculate_skill_hash(src_folder)
        
        from cryptography.hazmat.primitives.asymmetric import ed25519
        pub_key_bytes = bytes.fromhex("4f8ea93fc321099ce3d5f57c4ed2588cec782ae28d2e70f81b39e31377a247f8")
        pub_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_key_bytes)
        pub_key.verify(signature, skill_hash)
        return True
    except Exception:
        return False

def _sandbox_command_if_possible(command: str) -> str:
    """Wraps a shell command in bubblewrap or Landlock sandbox if available on Linux.
    
    Isolates file write access to the workspace and /tmp directories, and restricts
    access to sensitive system files.
    """
    # Allow explicit bypass via env var (useful for testing/host dev control)
    if os.environ.get("ADA_DISABLE_SANDBOX") == "1":
        return command
        
    workspace_dir = Path.cwd().resolve()
    
    # 1. Try Bubblewrap (bwrap)
    bwrap_path = shutil.which("bwrap")
    if bwrap_path:
        bwrap_args = [
            bwrap_path,
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/sbin", "/sbin",
            "--ro-bind", "/etc/alternatives", "/etc/alternatives",
            "--dir", "/tmp",
            "--dir", "/var",
            "--proc", "/proc",
            "--dev", "/dev",
            "--bind", str(workspace_dir), str(workspace_dir),
            "--chdir", str(workspace_dir),
            "--unshare-all",
            "--die-with-parent"
        ]
        bwrap_cmd_str = " ".join(shlex.quote(arg) for arg in bwrap_args)
        return f"{bwrap_cmd_str} -- bash -c {shlex.quote(command)}"
        
    # 2. Try Landlock
    try:
        import ctypes
        import ctypes.util
        libc_path = ctypes.util.find_library("c")
        if libc_path:
            libc = ctypes.CDLL(libc_path, use_errno=True)
            # Check SYS_LANDLOCK_CREATE_RULESET (syscall 445) support
            abi = libc.syscall(445, 0, 0, 1 << 0)
            if abi > 0:
                python_exe = sys.executable or "python3"
                landlock_runner = [
                    python_exe,
                    "-m", "agent.core.landlock",
                    str(workspace_dir),
                    "bash", "-c", command
                ]
                return " ".join(shlex.quote(arg) for arg in landlock_runner)
    except Exception:
        pass
        
    # 3. Fail closed if sandboxing is not available
    raise PermissionError("Security Exception: Sandbox environment could not be enforced (neither Bubblewrap nor Landlock is available). Halting command execution.")
