import os
import sys
import ctypes
import ctypes.util

# Load libc
try:
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
except Exception:
    libc = None

# Syscall numbers for x86_64
SYS_LANDLOCK_CREATE_RULESET = 445
SYS_LANDLOCK_ADD_RULE = 446
SYS_LANDLOCK_RESTRICT_SELF = 447

# Landlock rule type
LANDLOCK_RULE_PATH_BENEATH = 1

# Handled access flags (ABI v1)
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

LANDLOCK_ACCESS_FS_ALL = (1 << 13) - 1

class LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
    ]

class LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]

PR_SET_NO_NEW_PRIVS = 38

def prctl(option, arg2, arg3, arg4, arg5):
    if not libc:
        return -1
    return libc.prctl(option, ctypes.c_ulong(arg2), ctypes.c_ulong(arg3), ctypes.c_ulong(arg4), ctypes.c_ulong(arg5))

def apply_landlock(workspace_dir: str):
    if not libc:
        raise OSError("libc not loaded.")

    # Check if landlock is supported
    # Verify by calling create_ruleset with invalid inputs or check version
    # LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
    try:
        abi_version = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, 0, 0, 1 << 0)
    except Exception as e:
        raise OSError(f"Landlock syscall failed: {e}")

    if abi_version <= 0:
        raise OSError("Landlock is not supported by this kernel/system.")

    # Create ruleset
    attr = LandlockRulesetAttr()
    attr.handled_access_fs = LANDLOCK_ACCESS_FS_ALL
    
    ruleset_fd = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset_fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"Failed to create Landlock ruleset: {os.strerror(err)}")

    # Rules to add: (path, allowed_access_mask)
    rules = [
        # Workspace is fully readable & writable
        (workspace_dir, LANDLOCK_ACCESS_FS_ALL),
        # /tmp is writable for temporary files
        ("/tmp", LANDLOCK_ACCESS_FS_ALL),
        # System paths are read-only
        ("/usr", LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        ("/lib", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        ("/lib64", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        ("/bin", LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        ("/sbin", LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
        ("/etc", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR),
    ]

    for path, mask in rules:
        if not os.path.exists(path):
            continue
        try:
            # Open the path to get fd
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except Exception:
            continue

        rule = LandlockPathBeneathAttr()
        rule.allowed_access = mask
        rule.parent_fd = fd

        res = libc.syscall(SYS_LANDLOCK_ADD_RULE, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(rule), 0)
        os.close(fd)
        if res < 0:
            pass  # Ignore non-critical rule failure

    # Set no_new_privs (required to restrict self without CAP_SYS_ADMIN)
    if prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"Failed to set PR_SET_NO_NEW_PRIVS: {os.strerror(err)}")

    # Restrict self
    if libc.syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"Failed to restrict self: {os.strerror(err)}")

    # Close ruleset fd
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

    # Run the command
    try:
        # os.execvp executes the command and replaces the current process image
        os.execvp(cmd_args[0], cmd_args)
    except Exception as e:
        print(f"[Landlock] Failed to execute {cmd_args}: {e}", file=sys.stderr)
        sys.exit(127)
