"""Module for implementing Linux Landlock security sandboxing.

This module sets up system-level sandboxing using Linux Landlock (available in modern Linux
kernels) to restrict filesystem access of the running process to allowed directories.
"""

import os
import sys
import ctypes
import ctypes.util
from pathlib import Path
from typing import List, Tuple

# Freeze sandboxing bypass flag once at import time to prevent runtime manipulation
_ALLOW_UNSANDBOXED_EXECUTION_FROZEN = (os.environ.get("ALLOW_UNSANDBOXED_EXECUTION") == "true")

# Load libc dynamically to invoke low-level syscalls
try:
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
except Exception:
    libc = None

# Map Landlock syscall numbers (identical on x86_64 and arm64/aarch64 architectures)
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446

# Landlock rule type constants
LANDLOCK_RULE_PATH_BENEATH = 1

# Handled filesystem access flags (ABI v1)
LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12

# Mask combining all filesystem operations supported in ABI v1
LANDLOCK_ACCESS_FS_ALL = (1 << 13) - 1


class LandlockRulesetAttr(ctypes.Structure):
    """C struct representation of Landlock ruleset attributes.

    Defines the filesystem access rights handled by the ruleset.
    """
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
    ]


class LandlockPathBeneathAttr(ctypes.Structure):
    """C struct representation of a Landlock path rule.

    Specifies a parent directory and the access permissions permitted beneath it.
    """
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


# Process control constant to disable setting new privileges
PR_SET_NO_NEW_PRIVS = 38


def prctl(option: int, arg2: int, arg3: int, arg4: int, arg5: int) -> int:
    """Wrapper around the system prctl call to change process behaviors.

    Args:
        option: The prctl behavior option (e.g., PR_SET_NO_NEW_PRIVS).
        arg2: The first argument option.
        arg3: The second argument option.
        arg4: The third argument option.
        arg5: The fourth argument option.

    Returns:
        The syscall status result (usually 0 on success, or -1 on error).
    """
    if not libc:
        return -1
    return libc.prctl(
        option,
        ctypes.c_ulong(arg2),
        ctypes.c_ulong(arg3),
        ctypes.c_ulong(arg4),
        ctypes.c_ulong(arg5)
    )


def apply_landlock(workspace_dir: str) -> None:
    """Applies Linux Landlock sandbox restrictions to the current process.

    Restricts filesystem operations so that only specified directories (like the
    workspace directory and system directories with appropriate read/execute permissions)
    are accessible.

    Args:
        workspace_dir: The absolute path to the active agent workspace.

    Raises:
        OSError: If libc is not loaded, Landlock is not supported, or rule application fails.
    """
    if not libc:
        raise OSError("libc not loaded.")

    # Check if Landlock is supported by calling create_ruleset with version query flag
    try:
        abi_version = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, 0, 0, 1 << 0)
    except Exception as e:
        raise OSError(f"Landlock syscall failed: {e}")

    if abi_version <= 0:
        raise OSError("Landlock is not supported by this kernel/system.")

    # Set no_new_privs immediately at start of sandbox initialization (required to restrict self without CAP_SYS_ADMIN privileges)
    if prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"Failed to set PR_SET_NO_NEW_PRIVS: {os.strerror(err)}")

    # Convert workspace_dir to resolved absolute path to prevent relative path escapes
    workspace_dir_abs = str(Path(workspace_dir).resolve().absolute())

    # Handled filesystem access flags (ABI v1)
    # If ABI v2+ is supported, include LANDLOCK_ACCESS_FS_REFER
    LANDLOCK_ACCESS_FS_REFER = 1 << 13
    handled_access_fs = LANDLOCK_ACCESS_FS_ALL
    if abi_version >= 2:
        handled_access_fs |= LANDLOCK_ACCESS_FS_REFER

    # Create the initial Landlock ruleset
    attr = LandlockRulesetAttr()
    attr.handled_access_fs = handled_access_fs
    
    ruleset_fd = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset_fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"Failed to create Landlock ruleset: {os.strerror(err)}")

    # Create a unique, process-isolated sandbox temporary directory with strict 0700 permissions
    import uuid
    sandbox_tmp = f"/tmp/agent_sandbox_{uuid.uuid4()}"
    try:
        os.makedirs(sandbox_tmp, mode=0o700, exist_ok=True)
        os.environ["TMPDIR"] = sandbox_tmp
        os.environ["TEMP"] = sandbox_tmp
        os.environ["TMP"] = sandbox_tmp
    except Exception:
        sandbox_tmp = "/tmp"

    # Define path-specific rules: (path, allowed_access_mask)
    rules: List[Tuple[str, int]] = [
        # Workspace is fully readable & writable, but cannot execute binaries
        (workspace_dir_abs, handled_access_fs & ~LANDLOCK_ACCESS_FS_EXECUTE),
        # Unique sandbox temp directory is writable, but cannot execute binaries
        (sandbox_tmp, handled_access_fs & ~LANDLOCK_ACCESS_FS_EXECUTE),
        # Global /tmp is strictly read-only (no execution, no writes)
        ("/tmp", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        # System paths are read-only / execute-only
        ("/usr", LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        # /lib and /lib64 are strictly read-only (no execution, no writes)
        ("/lib", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        ("/lib64", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        ("/bin", LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        ("/sbin", LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        ("/etc", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
    ]

    # Append runtime environments (like Docker containers and configs) if they exist (read-only, no execution)
    for opt_path in ("/data", "/app", str(Path.home())):
        if os.path.exists(opt_path) and opt_path not in [r[0] for r in rules]:
            rules.append((opt_path, LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR))

    # Register each rule directory by opening its file descriptor and adding it to the ruleset
    for path, mask in rules:
        if not os.path.exists(path):
            continue
        try:
            # Open path with O_PATH to get a lightweight directory/file descriptor
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except Exception:
            continue

        rule = LandlockPathBeneathAttr()
        rule.allowed_access = mask
        rule.parent_fd = fd

        res = libc.syscall(SYS_LANDLOCK_ADD_RULE, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(rule), 0)
        os.close(fd)
        if res < 0:
            err = ctypes.get_errno()
            if path in [workspace_dir_abs, "/tmp"]:
                raise OSError(err, f"Failed to add critical Landlock rule for {path}: {os.strerror(err)}")
            pass  # Ignore non-critical rule failure

    # Apply the ruleset restrictions to the current thread/process
    if libc.syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"Failed to restrict self: {os.strerror(err)}")

    # Close ruleset file descriptor as restrictions are now active
    os.close(ruleset_fd)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 -m agent.core.landlock <workspace_dir> <command_args...>", file=sys.stderr)
        sys.exit(1)

    workspace = sys.argv[1]
    cmd_args = sys.argv[2:]

    try:
        apply_landlock(workspace)
    except Exception as e:
        if _ALLOW_UNSANDBOXED_EXECUTION_FROZEN:
            print(f"[Landlock] Warning: Sandbox not applied: {e}. ALLOW_UNSANDBOXED_EXECUTION is true, continuing unsandboxed.", file=sys.stderr)
        else:
            print(f"[Landlock] Error: Sandbox could not be enforced: {e}. Halting execution because ALLOW_UNSANDBOXED_EXECUTION is not true.", file=sys.stderr)
            sys.exit(126)

    # Scrub environment to prevent PATH injection and credential leakage
    # (Unlike sandbox_worker.py which runs inside bwrap, the Landlock path
    # does NOT have network namespace isolation, so we must be extra careful
    # about what environment the child process inherits.)
    safe_env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": os.environ.get("HOME", "/tmp"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    os.environ.clear()
    os.environ.update(safe_env)

    # Execute the requested command using absolute path to prevent PATH hijacking
    # NOTE: Landlock is filesystem-only — it does NOT provide network isolation.
    # Network egress must be controlled at a higher layer (bwrap, iptables, etc.)
    try:
        # Resolve command to absolute path if it's "bash" to avoid PATH lookup
        if cmd_args[0] == "bash":
            bash_abs = "/usr/bin/bash"
            if os.path.exists(bash_abs):
                cmd_args[0] = bash_abs
        os.execv(cmd_args[0], cmd_args)
    except Exception as e:
        print(f"[Landlock] Failed to execute {cmd_args}: {e}", file=sys.stderr)
        sys.exit(127)

