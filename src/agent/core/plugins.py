import sys
import importlib.util
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable
from types import ModuleType

def verify_plugin_ast_safety(plugin_path: Path) -> None:
    """Statically scans all Python files in the plugin package for unsafe calls."""
    import ast
    
    class SafetyVisitor(ast.NodeVisitor):
        ALLOWED_MODULES = {
            "typing", "fastapi", "pydantic", "datetime", "json", "pathlib", "uuid", "re",
            "asyncio", "logging", "math", "time", "agent", "google", "contextlib",
            "enum", "dataclasses", "types", "sqlite3", "urllib", "enuclea", "traceback",
            "fcntl", "sys", "os", "subprocess", "random", "playwright"
        }

        def __init__(self):
            self.errors = []
            self.os_aliases = {"os"}
            self.sub_aliases = {"subprocess"}
            
        def visit_Import(self, node):
            for name in node.names:
                parts = name.name.split(".")
                top_level = parts[0]
                if top_level not in self.ALLOWED_MODULES:
                    self.errors.append(f"Forbidden import: {name.name}")
                if name.name == "os":
                    self.os_aliases.add(name.asname or "os")
                elif name.name == "subprocess":
                    self.sub_aliases.add(name.asname or "subprocess")
            self.generic_visit(node)
            
        def visit_ImportFrom(self, node):
            if node.module:
                parts = node.module.split(".")
                top_level = parts[0]
                if top_level not in self.ALLOWED_MODULES:
                    self.errors.append(f"Forbidden import from module: {node.module}")
            
            forbidden_imports = {
                "os": {"system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"},
                "subprocess": {"run", "Popen", "call", "check_call", "check_output", "getstatusoutput", "getoutput"}
            }
            if node.module in forbidden_imports:
                for name in node.names:
                    if name.name in forbidden_imports[node.module]:
                        self.errors.append(f"Forbidden import: {name.name} from {node.module}")
            self.generic_visit(node)
            
        def visit_Call(self, node):
            forbidden_builtins = ("eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr", "hasattr")
            if isinstance(node.func, ast.Name):
                if node.func.id in forbidden_builtins:
                    self.errors.append(f"Forbidden dynamic built-in: {node.func.id}()")
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
                module_name = ""
                if isinstance(node.func.value, ast.Name):
                    module_name = node.func.value.id
                
                if module_name in self.os_aliases and func_name in ("system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"):
                    self.errors.append(f"Forbidden system call: {module_name}.{func_name}()")
                elif module_name in self.sub_aliases and func_name in ("run", "Popen", "call", "check_call", "check_output", "getstatusoutput", "getoutput"):
                    self.errors.append(f"Forbidden subprocess call: {module_name}.{func_name}()")
                elif func_name in forbidden_builtins:
                    self.errors.append(f"Forbidden call: .{func_name}()")
            self.generic_visit(node)

        def visit_Attribute(self, node):
            forbidden_attrs = ("__dict__", "__class__", "__bases__", "__subclasses__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__")
            if node.attr in forbidden_attrs:
                self.errors.append(f"Forbidden dynamic attribute access: .{node.attr}")
            self.generic_visit(node)

    for py_file in plugin_path.rglob("*.py"):
        try:
            with open(py_file, "r", encoding="utf-8", errors="replace") as f:
                code = f.read()
            tree = ast.parse(code, filename=str(py_file))
            visitor = SafetyVisitor()
            visitor.visit(tree)
            if visitor.errors:
                raise ValueError(f"AST safety check failed for {py_file.name}: {', '.join(visitor.errors)}")
        except SyntaxError as se:
            raise ValueError(f"AST syntax error in {py_file.name}: {se}")

class PluginState(str, Enum):
    DISCOVERED = "DISCOVERED"
    LOADING = "LOADING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"

@dataclass
class Plugin:
    name: str
    path: Path
    state: PluginState = PluginState.DISCOVERED
    error_message: Optional[str] = None
    module: Optional[ModuleType] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

# Global registry for custom scheduled task handlers (registered by plugins)
# Keys are task names, values are async callables: handler(prompt: str) -> None
_custom_scheduled_task_handlers: Dict[str, Callable] = {}

def register_scheduled_task_handler(name: str, handler: Callable):
    _custom_scheduled_task_handlers[name] = handler

class PluginManager:
    def __init__(self):
        self.plugins: Dict[str, Plugin] = {}

    def reset(self):
        """Reset the plugin manager state, clearing registered plugins and handlers (useful for tests)."""
        self.plugins.clear()
        _custom_scheduled_task_handlers.clear()

    def discover_plugins(self) -> Dict[str, Plugin]:
        """Discovers plugin packages in the configured plugin paths."""
        # Ensure project root is in sys.path for plugin imports
        _root = str(Path(__file__).resolve().parent.parent.parent.parent)
        if _root not in sys.path:
            sys.path.append(_root)

        # Try importing agent.plugins to get its __path__
        try:
            import agent.plugins
            plugin_paths = list(agent.plugins.__path__)
        except ImportError:
            # Fallback to local plugins folder if package structure is not initialized
            plugin_paths = [str(Path(__file__).resolve().parent.parent.parent / "plugins")]

        for path_str in plugin_paths:
            plugins_dir = Path(path_str)
            if not plugins_dir.exists() or not plugins_dir.is_dir():
                continue
            for item in plugins_dir.iterdir():
                if item.is_dir() and (item / "__init__.py").exists():
                    if item.name not in self.plugins:
                        metadata = {}
                        manifest_path = item / "manifest.json"
                        if manifest_path.exists():
                            try:
                                import json
                                with open(manifest_path, "r", encoding="utf-8") as f:
                                    metadata = json.load(f)
                            except Exception as e:
                                print(f"[PLUGINS] Failed to parse manifest.json for {item.name}: {e}")
                        
                        self.plugins[item.name] = Plugin(
                            name=item.name,
                            path=item,
                            state=PluginState.DISCOVERED,
                            metadata=metadata
                        )
        return self.plugins

    def load_plugins(self, app) -> None:
        """Dynamically loads core integrations and web routes from the plugins directories."""
        from agent import tools
        from agent import memory

        # Discover first
        self.discover_plugins()

        # Load dynamic platform configuration
        import os
        import json
        db_path = os.environ.get("AGENT_DB_PATH")
        if db_path:
            config_path = Path(db_path).parent / "platform_config.json"
        else:
            config_path = Path(os.getcwd()) / "data" / "platform_config.json"
            
        enabled_plugins = {}
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg_data = json.load(f)
                    enabled_plugins = cfg_data.get("plugins", {})
            except Exception:
                pass

        for name, plugin in self.plugins.items():
            if plugin.state == PluginState.ACTIVE:
                continue

            # Check if this plugin is explicitly disabled
            if enabled_plugins.get(name, True) is False:
                print(f"[PLUGINS] Plugin '{name}' is disabled in configuration. Skipping.")
                continue

            plugin.state = PluginState.LOADING
            try:
                # Perform AST safety check first
                verify_plugin_ast_safety(plugin.path)

                # Dynamic import package __init__.py
                spec = importlib.util.spec_from_file_location(f"agent.plugins.{name}", plugin.path / "__init__.py")
                module = importlib.util.module_from_spec(spec)

                # Ensure the intermediate 'agent.plugins' package is registered
                if "agent.plugins" not in sys.modules:
                    try:
                        import agent.plugins
                        sys.modules["agent.plugins"] = agent.plugins
                    except ImportError:
                        pass
                sys.modules[f"agent.plugins.{name}"] = module
                spec.loader.exec_module(module)

                plugin.module = module

                # Execute setup contract
                if hasattr(module, "setup_plugin"):
                    module.setup_plugin(
                        app=app,
                        register_tools=tools.register_plugin_tools,
                        register_scheduled_task=memory.ensure_plugin_scheduled_task
                    )
                    print(f"[PLUGINS] Successfully loaded plugin package '{name}'")
                plugin.state = PluginState.ACTIVE
            except Exception as e:
                import traceback
                plugin.state = PluginState.FAILED
                plugin.error_message = str(e)
                print(f"[PLUGINS] Failed to load plugin package '{name}': {e}")
                traceback.print_exc()

plugin_manager = PluginManager()
