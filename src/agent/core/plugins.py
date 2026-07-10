import os
import sys
import importlib.util
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable
from types import ModuleType

def verify_plugin_ast_safety(plugin_path: Path) -> None:
    """Statically scans all Python files in the plugin package for unsafe calls,
    unless the plugin has a valid cryptographic signature.
    """
    # 1. Try to verify the plugin's cryptographic signature first
    try:
        from agent.execution.tools.security import _verify_skill_signature
        if _verify_skill_signature(plugin_path):
            print(f"[PLUGINS] Plugin '{plugin_path.name}' verified cryptographically. Bypassing AST scan.")
            return
    except Exception as e:
        print(f"[PLUGINS] Cryptographic signature verification failed or missing for plugin '{plugin_path.name}': {e}")

    # 2. Fall back to AST safety checks on all Python files in the plugin package
    print(f"[PLUGINS] AST scanning plugin '{plugin_path.name}'...")
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
            plugin_paths = [str(Path(__file__).resolve().parent.parent.parent.parent / "plugins")]

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

        # Save app reference for dynamic lazy loading
        self._app = app

        # Discover first
        self.discover_plugins()

        # Load dynamic platform configuration from unified settings
        from agent.core.config import settings
        disabled_plugins = settings.disabled_plugins
        lazy_plugins = getattr(settings, "lazy_plugins", ["playwright"])

        for name, plugin in self.plugins.items():
            if plugin.state == PluginState.ACTIVE:
                continue

            # Check if this plugin is explicitly disabled
            if name in disabled_plugins:
                print(f"[PLUGINS] Plugin '{name}' is disabled in configuration. Skipping.")
                continue

            # Check if this plugin is lazy
            is_lazy = name in lazy_plugins or plugin.metadata.get("lazy") is True
            if is_lazy:
                # Do not load on startup; register lazy wrappers for its tools
                plugin.state = PluginState.DISCOVERED
                manifest_tools = plugin.metadata.get("tools", [])
                lazy_wrappers = []
                for tool_meta in manifest_tools:
                    tool_name = tool_meta.get("name")
                    if tool_name:
                        try:
                            wrapper = self._create_lazy_tool_wrapper(name, tool_name)
                            lazy_wrappers.append(wrapper)
                        except Exception as e:
                            print(f"[PLUGINS] Failed to create lazy wrapper for tool {tool_name} of plugin {name}: {e}")
                
                if lazy_wrappers:
                    tools.register_plugin_tools(lazy_wrappers)
                    print(f"[PLUGINS] Registered lazy wrappers for {name} plugin: {[w.__name__ for w in lazy_wrappers]}")
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

    def _create_lazy_tool_wrapper(self, plugin_name: str, tool_name: str) -> Callable:
        import inspect
        import importlib

        try:
            tools_mod = importlib.import_module(f"agent.plugins.{plugin_name}.tools")
            real_tool = getattr(tools_mod, tool_name)
        except Exception as e:
            # Fallback if module cannot be imported (e.g. missing dependencies)
            print(f"[PLUGINS] Warning: Could not import tool {tool_name} from plugin {plugin_name} for signature: {e}")
            async def fallback_lazy_tool(*args, **kwargs):
                self.load_single_plugin(plugin_name)
                tools_mod = importlib.import_module(f"agent.plugins.{plugin_name}.tools")
                real_tool = getattr(tools_mod, tool_name)
                return await real_tool(*args, **kwargs)
            fallback_lazy_tool.__name__ = tool_name
            return fallback_lazy_tool

        async def lazy_tool_wrapper(*args, **kwargs):
            self.load_single_plugin(plugin_name)
            tools_mod = importlib.import_module(f"agent.plugins.{plugin_name}.tools")
            real_tool = getattr(tools_mod, tool_name)
            return await real_tool(*args, **kwargs)

        lazy_tool_wrapper.__name__ = real_tool.__name__
        lazy_tool_wrapper.__doc__ = real_tool.__doc__
        lazy_tool_wrapper.__signature__ = inspect.signature(real_tool)
        return lazy_tool_wrapper

    def load_single_plugin(self, name: str) -> None:
        """Loads a single plugin dynamically (for lazy-loading)."""
        plugin = self.plugins.get(name)
        if not plugin or plugin.state == PluginState.ACTIVE:
            return

        plugin.state = PluginState.LOADING
        try:
            # Perform AST safety check first
            verify_plugin_ast_safety(plugin.path)

            # Ensure the intermediate 'agent.plugins' package is registered
            if "agent.plugins" not in sys.modules:
                try:
                    import agent.plugins
                    sys.modules["agent.plugins"] = agent.plugins
                except ImportError:
                    pass

            spec = importlib.util.spec_from_file_location(f"agent.plugins.{name}", plugin.path / "__init__.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[f"agent.plugins.{name}"] = module
            spec.loader.exec_module(module)

            plugin.module = module

            if hasattr(module, "setup_plugin"):
                from agent import tools
                from agent import memory
                app = getattr(self, "_app", None)
                module.setup_plugin(
                    app=app,
                    register_tools=tools.register_plugin_tools,
                    register_scheduled_task=memory.ensure_plugin_scheduled_task
                )
                print(f"[PLUGINS] Dynamically loaded lazy plugin package '{name}'")
            plugin.state = PluginState.ACTIVE
        except Exception as e:
            import traceback
            plugin.state = PluginState.FAILED
            plugin.error_message = str(e)
            print(f"[PLUGINS] Failed to dynamically load plugin package '{name}': {e}")
            traceback.print_exc()
            raise e

plugin_manager = PluginManager()
