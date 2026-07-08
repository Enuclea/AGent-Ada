"""Module for consolidating AST safety checks for dynamic plugins and skills.

Statically analyzes Python source code to detect and reject unsafe imports,
dynamic execution vectors, namespace extraction attempts, and filesystem breakouts.
"""

import ast
from pathlib import Path
from typing import Set

ALLOWED_MODULES: Set[str] = {
    "typing", "fastapi", "pydantic", "datetime", "json", "pathlib", "uuid", "re",
    "asyncio", "logging", "math", "time", "agent", "google", "contextlib",
    "enum", "dataclasses", "types", "sqlite3", "urllib", "enuclea", "traceback",
    "fcntl", "sys", "random", "playwright", "googleapiclient", "google_auth_oauthlib",
    "base64", "secrets", "email"
}

class SafetyVisitor(ast.NodeVisitor):
    """AST visitor that enforces standard library restrictions, blocks dynamic builtins,

    and prevents namespace/module extraction via sys.modules or dynamic imports.
    """

    def __init__(self) -> None:
        self.errors = []
        self.sys_names = {"sys"}

    def visit_Import(self, node: ast.Import) -> None:
        for name in node.names:
            parts = name.name.split(".")
            top_level = parts[0]
            if top_level not in ALLOWED_MODULES:
                self.errors.append(f"Forbidden import: {name.name}")
            if name.name == "sys":
                self.sys_names.add(name.asname or name.name)
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
                self.errors.append(f"Forbidden import from module: {node.module}")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Name) and node.value.id in self.sys_names:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.sys_names.add(target.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        forbidden_builtins = (
            "eval", "exec", "compile", "__import__", "getattr", "setattr",
            "delattr", "hasattr", "vars", "globals", "locals"
        )
        
        # Check direct calls
        if isinstance(node.func, ast.Name):
            if node.func.id in forbidden_builtins:
                self.errors.append(f"Forbidden dynamic built-in: {node.func.id}()")
                
        # Check attribute calls (e.g. os.system(), subprocess.run(), pickle.load())
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
            if func_name in forbidden_builtins:
                self.errors.append(f"Forbidden call: .{func_name}()")
            
            # Helper to check for module reference
            val_name = ""
            if isinstance(node.func.value, ast.Name):
                val_name = node.func.value.id
            
            # Block subprocess functions
            if val_name == "subprocess" or func_name in ("run", "Popen", "call", "check_call", "check_output", "getstatusoutput", "getoutput"):
                if val_name in ("", "subprocess"):
                    self.errors.append(f"Forbidden subprocess call: {func_name}()")
                    
            # Block os execution functions
            if val_name == "os" and func_name in ("system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"):
                self.errors.append(f"Forbidden os call: os.{func_name}()")
                
            # Block pickle & shelve serialization exploits
            if val_name in ("pickle", "shelve") or func_name in ("load", "loads", "Unpickler", "open"):
                if val_name in ("", "pickle", "shelve"):
                    self.errors.append(f"Forbidden serialization call: {func_name}()")
                    
            # Block dynamic importlib execution
            if val_name == "importlib" or func_name in ("import_module", "__import__"):
                if val_name in ("", "importlib"):
                    self.errors.append(f"Forbidden dynamic import call: {func_name}()")

            # Block urllib/urllib.request network request functions
            if val_name in ("urllib", "request") or func_name in ("urlopen", "urlretrieve"):
                if val_name in ("", "urllib", "request", "urllib.request"):
                    self.errors.append(f"Forbidden network request call: {func_name}()")

            # Block sqlite3 database connection breakouts
            if val_name == "sqlite3" and func_name == "connect":
                self.errors.append("Forbidden sqlite3 database connection call: connect()")

        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "__builtins__":
            self.errors.append("Forbidden access to name: __builtins__")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        forbidden_attrs = (
            "__dict__", "__class__", "__bases__", "__subclasses__",
            "__getattribute__", "__getattr__", "__setattr__", "__delattr__"
        )
        if node.attr in forbidden_attrs:
            self.errors.append(f"Forbidden dynamic attribute access: .{node.attr}")
        
        def is_sys_ref(val_node) -> bool:
            if isinstance(val_node, ast.Name):
                return val_node.id in self.sys_names
            if isinstance(val_node, ast.Attribute):
                return val_node.attr == "sys" or is_sys_ref(val_node.value)
            return False

        if node.attr == "modules" and is_sys_ref(node.value):
            self.errors.append("Forbidden attribute access: sys.modules")
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
