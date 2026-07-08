import sys
import importlib.util
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable
from types import ModuleType

def verify_plugin_ast_safety(plugin_path: Path) -> None:
    """Statically scans all Python files in the plugin package for unsafe calls,
    unless the plugin has a valid cryptographic signature from the developer.
    """
    try:
        from agent.execution.tools.security import _verify_skill_signature
        if _verify_skill_signature(plugin_path):
            return
    except Exception:
        pass

    from agent.security.ast_safety import verify_ast_safety
    for py_file in plugin_path.rglob("*.py"):
        with open(py_file, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
        verify_ast_safety(code, str(py_file))

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

        # Load dynamic platform configuration from unified settings
        from agent.core.config import settings
        disabled_plugins = settings.disabled_plugins

        for name, plugin in self.plugins.items():
            if plugin.state == PluginState.ACTIVE:
                continue

            # Check if this plugin is explicitly disabled
            if name in disabled_plugins:
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
