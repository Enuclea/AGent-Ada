import os
import sys
import shutil
import shlex
from pathlib import Path

# Freeze sandboxing bypass flag once at import time to prevent runtime manipulation
_ADA_DISABLE_SANDBOX_FROZEN = (os.environ.get("ADA_DISABLE_SANDBOX") == "1")
_ALLOW_UNSANDBOXED_EXECUTION_FROZEN = (os.environ.get("ALLOW_UNSANDBOXED_EXECUTION") == "true")

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
        except Exception as e:
            raise IOError(f"Failed to read file {p} during hash calculation: {e}")
            
    return hasher.digest()

class TrustedKeysList(list):
    def append(self, item):
        import sys
        if not (os.environ.get("TESTING") == "1" and "pytest" in sys.modules):
            raise PermissionError("Security Exception: Cannot modify trusted keys outside of test environment.")
        super().append(item)
        
    def extend(self, items):
        import sys
        if not (os.environ.get("TESTING") == "1" and "pytest" in sys.modules):
            raise PermissionError("Security Exception: Cannot modify trusted keys outside of test environment.")
        super().extend(items)
        
_additional_trusted_keys = TrustedKeysList()

def _verify_skill_signature(src_folder: Path) -> bool:
    sig_path = src_folder / "signature.sig"
    if not sig_path.exists():
        return False
        
    with open(sig_path, "rb") as f:
        signature = f.read()
        
    skill_hash = _calculate_skill_hash(src_folder)
    
    from cryptography.hazmat.primitives.asymmetric import ed25519
    import sys
    trusted_keys = []
    if os.environ.get("TESTING") == "1" and "pytest" in sys.modules:
        trusted_keys.extend(_additional_trusted_keys)
    from agent.core.config import DEVELOPER_PUBLIC_KEY
    trusted_keys.append(DEVELOPER_PUBLIC_KEY)
    
    last_err = None
    for pub_key_hex in trusted_keys:
        try:
            pub_key_bytes = bytes.fromhex(pub_key_hex)
            pub_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_key_bytes)
            pub_key.verify(signature, skill_hash)
            return True
        except Exception as e:
            last_err = e
            continue
    raise ValueError(f"Signature verification failed: {last_err}")

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
    import sys
    trusted_keys = []
    if os.environ.get("TESTING") == "1" and "pytest" in sys.modules:
        trusted_keys.extend(_additional_trusted_keys)
    from agent.core.config import DEVELOPER_PUBLIC_KEY
    trusted_keys.append(DEVELOPER_PUBLIC_KEY)
    
    last_err = None
    for pub_key_hex in trusted_keys:
        try:
            pub_key_bytes = bytes.fromhex(pub_key_hex)
            pub_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_key_bytes)
            pub_key.verify(signature, skill_hash)
            return True
        except Exception as e:
            last_err = e
            continue
    raise ValueError(f"Signature verification failed: {last_err}")

from typing import List

def _sandbox_command_if_possible(
    command: str, 
    require_network_isolation: bool = False,
    read_only_workspace: bool = False,
    bind_paths: List[str] = None
) -> List[str]:
    """Wraps a shell command in bubblewrap or Landlock sandbox if available on Linux.
    
    Isolates file write access to the workspace and /tmp directories, and restricts
    access to sensitive system files.
    """
    # Allow explicit bypass via env var (useful for testing/host dev control)
    if _ADA_DISABLE_SANDBOX_FROZEN:
        return ["bash", "-c", command]

    # Check if running on Windows OS
    if sys.platform == "win32":
        if require_network_isolation:
            raise PermissionError(
                "Security Exception: Sandbox environment could not be enforced with network isolation on Windows. "
                "Windows does not support Bubblewrap network namespace isolation."
            )
        if _ALLOW_UNSANDBOXED_EXECUTION_FROZEN:
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
        workspace_bind_flag = "--ro-bind" if (require_network_isolation or read_only_workspace) else "--bind"
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
            workspace_bind_flag, str(workspace_dir), str(workspace_dir),
            "--chdir", str(workspace_dir),
            "--die-with-parent"
        ]
        
        if require_network_isolation:
            bwrap_args.append("--unshare-all")
        else:
            bwrap_args += [
                "--unshare-ipc",
                "--unshare-pid",
                "--unshare-uts",
                "--unshare-cgroup"
            ]
            if os.path.exists("/etc/resolv.conf"):
                bwrap_args += ["--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf"]
            if os.path.exists("/etc/hosts"):
                bwrap_args += ["--ro-bind", "/etc/hosts", "/etc/hosts"]
            for cert_dir in ["/etc/ssl", "/etc/ca-certificates", "/etc/pki", "/usr/share/ca-certificates"]:
                if os.path.exists(cert_dir):
                    bwrap_args += ["--ro-bind", cert_dir, cert_dir]
                
        if bind_paths:
            for bp in bind_paths:
                if isinstance(bp, tuple):
                    path_str, is_writable = bp
                else:
                    path_str, is_writable = bp, False
                bp_path = Path(path_str).resolve()
                if bp_path.exists():
                    bind_flag = "--bind" if is_writable else "--ro-bind"
                    bwrap_args += [bind_flag, str(bp_path), str(bp_path)]
                    
        return bwrap_args + ["--", "bash", "-c", command]
        
    # If strict network isolation is required, we cannot fall back to Landlock (filesystem-only)
    if require_network_isolation:
        raise PermissionError(
            "Security Exception: Sandbox environment could not be enforced with network isolation. "
            "Bubblewrap (bwrap) is not available, and Landlock does not support network namespace isolation."
        )

    # 2. Try Landlock (IMPORTANT: Landlock provides filesystem-only isolation.
    # Unlike Bubblewrap, it does NOT restrict network access. Commands running
    # under the Landlock fallback can still make outbound network requests.
    # If network isolation is required, Bubblewrap must be installed.)
    try:
        import ctypes
        import ctypes.util
        libc_path = ctypes.util.find_library("c")
        if libc_path:
            libc = ctypes.CDLL(libc_path, use_errno=True)
            # Check SYS_LANDLOCK_CREATE_RULESET (syscall 444) support
            abi = libc.syscall(444, 0, 0, 1 << 0)
            if abi > 0:
                print("[Security] Warning: Falling back to Landlock sandbox. "
                      "Landlock provides filesystem isolation ONLY — network "
                      "access is NOT restricted. Install Bubblewrap (bwrap) for "
                      "full isolation.", file=sys.stderr)
                python_exe = sys.executable or "python3"
                landlock_runner = [
                    python_exe,
                    "-m", "agent.core.landlock",
                    str(workspace_dir),
                    "/usr/bin/bash", "-c", command
                ]
                return landlock_runner
    except Exception:
        pass
        
    # 3. Fail closed if sandboxing is not available
    raise PermissionError("Security Exception: Sandbox environment could not be enforced (neither Bubblewrap nor Landlock is available). Halting command execution.")
