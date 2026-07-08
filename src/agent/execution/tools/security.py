import os
import sys
import shutil
import shlex
from pathlib import Path

# Freeze sandboxing bypass flag once at import time to prevent runtime manipulation
_ADA_DISABLE_SANDBOX_FROZEN = (os.environ.get("ADA_DISABLE_SANDBOX") == "1")

def _is_safe_path(base_dir, path) -> bool:
    """Helper that resolves absolute paths and verifies that target path resides strictly within base_dir,
    recursively checking that no symlinks in the path resolve outside base_dir.
    """
    from agent.security.path_utils import is_safe_path
    return is_safe_path(Path(base_dir), Path(path))

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
        
    with open(sig_path, "rb") as f:
        signature = f.read()
        
    skill_hash = _calculate_skill_hash(src_folder)
    
    from cryptography.hazmat.primitives.asymmetric import ed25519
    pub_key_hex = os.environ.get("ADA_SKILL_PUBLIC_KEY")
    if not pub_key_hex:
        raise ValueError("ADA_SKILL_PUBLIC_KEY environment variable must be set for signature verification.")
    try:
        pub_key_bytes = bytes.fromhex(pub_key_hex)
        pub_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_key_bytes)
        pub_key.verify(signature, skill_hash)
        return True
    except Exception as e:
        raise ValueError(f"Signature verification failed: {e}")

def _calculate_in_memory_hash(files_dict: dict) -> bytes:
    import hashlib
    hasher = hashlib.sha256()
    sorted_keys = sorted(files_dict.keys())
    for rel_path in sorted_keys:
        if rel_path != "signature.sig" and not Path(rel_path).name.startswith('.'):
            hasher.update(rel_path.encode('utf-8'))
            hasher.update(files_dict[rel_path])
    return hasher.digest()

def _verify_in_memory_signature(files_dict: dict) -> bool:
    if "signature.sig" not in files_dict:
        return False
    signature = files_dict["signature.sig"]
    skill_hash = _calculate_in_memory_hash(files_dict)
    
    from cryptography.hazmat.primitives.asymmetric import ed25519
    pub_key_hex = os.environ.get("ADA_SKILL_PUBLIC_KEY")
    if not pub_key_hex:
        raise ValueError("ADA_SKILL_PUBLIC_KEY environment variable must be set for signature verification.")
    try:
        pub_key_bytes = bytes.fromhex(pub_key_hex)
        pub_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_key_bytes)
        pub_key.verify(signature, skill_hash)
        return True
    except Exception as e:
        raise ValueError(f"Signature verification failed: {e}")

from typing import List

def _sandbox_command_if_possible(command: str) -> List[str]:
    """Wraps a shell command in bubblewrap or Landlock sandbox if available on Linux.
    
    Isolates file write access to the workspace and /tmp directories, and restricts
    access to sensitive system files.
    """
    # Allow explicit bypass via env var (useful for testing/host dev control)
    if _ADA_DISABLE_SANDBOX_FROZEN:
        return ["bash", "-c", command]

    # Check if running on Windows OS
    if sys.platform == "win32":
        if os.environ.get("ALLOW_UNSANDBOXED_EXECUTION") == "true":
            print("[Security] Warning: Running on Windows without filesystem sandboxing. Sandbox restrictions are disabled.", file=sys.stderr)
            return ["cmd.exe", "/c", command]
        raise PermissionError(
            "Security Exception: Filesystem sandboxing (Bubblewrap/Landlock) is not supported on Windows. "
            "To run tools unsandboxed on Windows, you must explicitly acknowledge this by setting "
            "ALLOW_UNSANDBOXED_EXECUTION=true in your environment or configuration."
        )
        
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
        return bwrap_args + ["--", "bash", "-c", command]
        
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
                return landlock_runner
    except Exception:
        pass
        
    # 3. Fail closed if sandboxing is not available
    raise PermissionError("Security Exception: Sandbox environment could not be enforced (neither Bubblewrap nor Landlock is available). Halting command execution.")
