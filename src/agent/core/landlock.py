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

# Load libc dynamically to invoke low-level syscalls
try:
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
except Exception:
    libc = None

# Syscall numbers for x86_64 Linux architecture
SYS_LANDLOCK_CREATE_RULESET = 445
SYS_LANDLOCK_ADD_RULE = 446
SYS_LANDLOCK_RESTRICT_SELF = 447

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

    # Create the initial Landlock ruleset
    attr = LandlockRulesetAttr()
    attr.handled_access_fs = LANDLOCK_ACCESS_FS_ALL
    
    ruleset_fd = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset_fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"Failed to create Landlock ruleset: {os.strerror(err)}")

    # Define path-specific rules: (path, allowed_access_mask)
    rules: List[Tuple[str, int]] = [
        # Workspace is fully readable & writable
        (workspace_dir, LANDLOCK_ACCESS_FS_ALL),
        # /tmp is writable for temporary files
        ("/tmp", LANDLOCK_ACCESS_FS_ALL),
        # System paths are read-only / execute-only
        ("/usr", LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        ("/lib", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        ("/lib64", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        ("/bin", LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        ("/sbin", LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        ("/etc", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
    ]

    # Append runtime environments (like Docker containers and configs) if they exist
    for opt_path in ("/data", "/app", str(Path.home())):
        if os.path.exists(opt_path) and opt_path not in [r[0] for r in rules]:
            rules.append((opt_path, LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR))

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
            if path in [workspace_dir, "/tmp"]:
                raise OSError(err, f"Failed to add critical Landlock rule for {path}: {os.strerror(err)}")
            pass  # Ignore non-critical rule failure

    # Set no_new_privs (required to restrict self without CAP_SYS_ADMIN privileges)
    if prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"Failed to set PR_SET_NO_NEW_PRIVS: {os.strerror(err)}")

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
        print(f"[Landlock] Warning: Sandbox not applied: {e}", file=sys.stderr)

    # Execute the requested command, replacing the current process image
    try:
        os.execvp(cmd_args[0], cmd_args)
    except Exception as e:
        print(f"[Landlock] Failed to execute {cmd_args}: {e}", file=sys.stderr)
        sys.exit(127)
