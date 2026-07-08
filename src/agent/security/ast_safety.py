"""Module for consolidating AST safety checks for dynamic plugins and skills.

Statically analyzes Python source code to detect and reject unsafe imports,
dynamic execution vectors, namespace extraction attempts, and filesystem breakouts.
"""

import ast
from pathlib import Path
from typing import Set

ALLOWED_MODULES: Set[str] = {
    "typing", "fastapi", "pydantic", "datetime", "json", "pathlib", "uuid", "re",
    "asyncio", "logging", "math", "time", "contextlib",
    "enum", "dataclasses", "types", "traceback",
    "fcntl", "random", "base64", "secrets", "email"
}

ALLOWED_SUBMODULES: Set[str] = {
    "google.oauth2", "google.oauth2.credentials", "google.auth.transport.requests",
    "google.auth", "google.protobuf", "googleapiclient.discovery",
    "google_auth_oauthlib.flow", "playwright.async_api"
}

SAFE_BUILTINS: Set[str] = {
    "abs", "all", "any", "ascii", "bin", "bool", "bytes", "bytearray", "callable", "chr",
    "dict", "divmod", "enumerate", "filter", "float", "format", "frozenset",
    "hash", "hex", "int", "isinstance", "issubclass", "iter", "len", "list",
    "map", "max", "min", "next", "oct", "ord", "pow", "print", "range", "repr",
    "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip"
}

FORBIDDEN_CALLABLE_PATHS: Set[str] = {
    # Subprocess / Execution
    "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.getstatusoutput", "subprocess.getoutput",
    "os.system", "os.popen", "os.spawn", "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe",
    "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe", "os.execve", "os.execl", "os.execvp",
    "os.posix_spawn", "os.posix_spawnp",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
    "asyncio.subprocess.create_subprocess_exec", "asyncio.subprocess.create_subprocess_shell",
    # Dynamic Code Execution
    "eval", "exec", "compile", "__import__", "importlib.import_module",
    "getattr", "setattr", "delattr", "hasattr", "globals", "locals", "vars",
    # File System access breakouts
    "open", "io.open", "os.open", "os.fdopen",
    # SQL injection breakouts
    "sqlite3.connect",
    # Network requests
    "urllib.request.urlopen", "urllib.request.urlretrieve", "http.client.HTTPConnection",
    "http.client.HTTPSConnection", "socket.socket", "socket.connect"
}

class SafetyVisitor(ast.NodeVisitor):
    """AST visitor that enforces standard library restrictions, blocks dynamic builtins,

    and prevents namespace/module extraction via sys.modules or dynamic imports.
    """

    def __init__(self) -> None:
        self.errors = []
        self.sys_names = {"sys"}
        self.aliases = {}

    def resolve_attr_path(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        elif isinstance(node, ast.Attribute):
            base = self.resolve_attr_path(node.value)
            if base:
                return f"{base}.{node.attr}"
            return node.attr
        elif isinstance(node, ast.Subscript):
            base = self.resolve_attr_path(node.value)
            if isinstance(node.slice, ast.Constant):
                return f"{base}[{node.slice.value!r}]"
            elif isinstance(node.slice, ast.Index) and isinstance(node.slice.value, ast.Constant):
                return f"{base}[{node.slice.value.value!r}]"
            return f"{base}[]"
        elif isinstance(node, ast.Call):
            base = self.resolve_attr_path(node.func)
            return f"{base}()"
        return ""

    def visit_Import(self, node: ast.Import) -> None:
        for name in node.names:
            parts = name.name.split(".")
            top_level = parts[0]
            if top_level not in ALLOWED_MODULES:
                is_submodule_allowed = False
                for sub in ALLOWED_SUBMODULES:
                    if name.name == sub or name.name.startswith(sub + "."):
                        is_submodule_allowed = True
                        break
                if not is_submodule_allowed:
                    self.errors.append(f"Forbidden import: {name.name}")
            if name.name == "sys":
                self.sys_names.add(name.asname or name.name)
            local_name = name.asname or name.name
            self.aliases[local_name] = name.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            parts = node.module.split(".")
            top_level = parts[0]
            if top_level == "os":
                # Strictly allow only 'environ' and 'getenv' from 'os'
                for name in node.names:
                    if name.name not in {"environ", "getenv"}:
                        self.errors.append(f"Forbidden import: {name.name} from os")
            elif top_level == "sys":
                for name in node.names:
                    if name.name in ("modules", "*"):
                        self.errors.append(f"Forbidden import: {name.name} from sys")
            elif top_level not in ALLOWED_MODULES:
                is_submodule_allowed = False
                for sub in ALLOWED_SUBMODULES:
                    if node.module == sub or node.module.startswith(sub + "."):
                        is_submodule_allowed = True
                        break
                if not is_submodule_allowed:
                    self.errors.append(f"Forbidden import from module: {node.module}")
            
            for name in node.names:
                local_name = name.asname or name.name
                self.aliases[local_name] = f"{node.module}.{name.name}"
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Name):
            val_resolved = self.aliases.get(node.value.id, node.value.id)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.aliases[target.id] = val_resolved
                    if node.value.id in self.sys_names:
                        self.sys_names.add(target.id)
        elif isinstance(node.value, ast.Attribute):
            val_resolved = self.resolve_attr_path(node.value)
            if val_resolved:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.aliases[target.id] = val_resolved
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # Check direct calls
        if isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLABLE_PATHS:
                if node.func.id in {"eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr", "hasattr", "vars", "globals", "locals"}:
                    self.errors.append(f"Forbidden dynamic built-in: {node.func.id}()")
                else:
                    self.errors.append(f"Forbidden call: {node.func.id}()")
                
        # Resolve target function call path
        resolved_path = self.resolve_attr_path(node.func)
        if resolved_path:
            # Check if resolved_path matches any forbidden callable paths
            if resolved_path in FORBIDDEN_CALLABLE_PATHS:
                parts = resolved_path.split(".")
                func_name = parts[-1]
                if func_name in {"eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr", "hasattr", "vars", "globals", "locals"}:
                    self.errors.append(f"Forbidden dynamic built-in: {func_name}()")
                else:
                    self.errors.append(f"Forbidden call: {resolved_path}()")
                
            # Block any method/function name matches that are in FORBIDDEN_CALLABLE_PATHS
            parts = resolved_path.split(".")
            func_name = parts[-1]
            if func_name in FORBIDDEN_CALLABLE_PATHS:
                # Check if we already reported a forbidden call/built-in for this node.func
                # (to avoid duplicate errors on the same call node)
                already_reported = False
                for err in self.errors:
                    if f"{func_name}()" in err:
                        already_reported = True
                        break
                if not already_reported:
                    if func_name in {"eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr", "hasattr", "vars", "globals", "locals"}:
                        self.errors.append(f"Forbidden dynamic built-in: {func_name}()")
                    else:
                        self.errors.append(f"Forbidden call: {resolved_path}()")
                
            # Fallback substring checks for defense-in-depth subprocess/execution gating
            path_lower = resolved_path.lower()
            if any(term in path_lower for term in ("subprocess", "popen", "execve", "execl", "execvp", "posix_spawn", "create_subprocess")):
                self.errors.append(f"Forbidden subprocess/execution call: {resolved_path}()")
            if "pickle" in path_lower or "shelve" in path_lower or "marshal" in path_lower or "yaml.load" in path_lower:
                self.errors.append(f"Forbidden serialization call: {resolved_path}()")
            if "urllib" in path_lower or "urlopen" in path_lower or "urlretrieve" in path_lower:
                self.errors.append(f"Forbidden network request call: {resolved_path}()")
            if "sqlite3" in path_lower or "connect" in path_lower:
                if "sqlite3" in path_lower or not parts[0]:
                    self.errors.append(f"Forbidden sqlite3 database connection call: {resolved_path}()")

        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "__builtins__":
            self.errors.append("Forbidden access to name: __builtins__")
        import builtins
        if hasattr(builtins, node.id):
            if node.id not in SAFE_BUILTINS:
                self.errors.append(f"Forbidden builtin access: {node.id}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        forbidden_attrs = (
            "__dict__", "__class__", "__bases__", "__subclasses__",
            "__getattribute__", "__getattr__", "__setattr__", "__delattr__",
            "_getframe", "modules", "__globals__", "__code__", "__closure__",
            "ctypes", "cffi", "mmap"
        )
        if node.attr in forbidden_attrs:
            self.errors.append(f"Forbidden dynamic attribute access: .{node.attr}")
            if node.attr == "modules":
                self.errors.append("Forbidden attribute access: sys.modules")
        
        def is_sys_ref(val_node) -> bool:
            if isinstance(val_node, ast.Name):
                return val_node.id in self.sys_names
            if isinstance(val_node, ast.Attribute):
                return val_node.attr == "sys" or is_sys_ref(val_node.value)
            return False

        if node.attr == "modules" and is_sys_ref(node.value):
            self.errors.append("Forbidden attribute access: sys.modules")

        resolved_path = self.resolve_attr_path(node)
        if resolved_path:
            if "sys.modules" in resolved_path:
                self.errors.append("Forbidden attribute access: sys.modules")
            if "sqlite3" in resolved_path:
                self.errors.append(f"Forbidden sqlite3 access: {resolved_path}")
            if "urllib" in resolved_path:
                self.errors.append(f"Forbidden network/urllib access: {resolved_path}")
            if "subprocess" in resolved_path:
                self.errors.append(f"Forbidden subprocess access: {resolved_path}")
            if any(f"os.{term}" in resolved_path for term in ("system", "popen", "spawn")):
                self.errors.append(f"Forbidden os execution access: {resolved_path}")
            if any(term in resolved_path for term in ("execve", "execl", "execvp", "spawn")):
                self.errors.append(f"Forbidden execution access: {resolved_path}")
            if "ctypes" in resolved_path or "cffi" in resolved_path:
                self.errors.append(f"Forbidden library access: {resolved_path}")
            if "mmap" in resolved_path:
                self.errors.append(f"Forbidden mmap access: {resolved_path}")
            if any(term in resolved_path for term in ("pickle", "shelve", "marshal", "yaml.load")):
                self.errors.append(f"Forbidden serialization access: {resolved_path}")
            if "importlib" in resolved_path:
                self.errors.append(f"Forbidden dynamic import access: {resolved_path}")
                
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            import re
            if re.search(r"\battach\b", node.value, re.IGNORECASE):
                self.errors.append("Forbidden SQL ATTACH command in string constant")
        self.generic_visit(node)

    def visit_Str(self, node: ast.Str) -> None:
        if isinstance(node.s, str):
            import re
            if re.search(r"\battach\b", node.s, re.IGNORECASE):
                self.errors.append("Forbidden SQL ATTACH command in string literal")
        self.generic_visit(node)


def verify_ast_safety(code: str, filename: str) -> None:
    """Parses python source code and scans it using SafetyVisitor to verify AST security.

    Raises:
        ValueError: If there are compilation syntax errors or security policy violations.
    """
    try:
        tree = ast.parse(code, filename=filename)
        visitor = SafetyVisitor()
        visitor.visit(tree)
        if visitor.errors:
            raise ValueError(f"AST safety check failed for {Path(filename).name}: {', '.join(visitor.errors)}")
    except SyntaxError as se:
        raise ValueError(f"AST syntax error in {Path(filename).name}: {se}")
