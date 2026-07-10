import os
import sys
import importlib.util
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable
from types import ModuleType

def verify_plugin_ast_safety(plugin_path: Path) -> dict:
    """Statically scans all Python files in the plugin package for unsafe calls.
    
    For signed plugins, the AST scan still runs as advisory logging (warnings only)
    rather than being skipped entirely. This provides operator visibility into what
    signed code does, while the signature bypasses the rejection gate.
    
    Returns the in-memory plugin_files dict {rel_path: bytes} for TOCTOU-safe
    checksum verification downstream.
    """
    # 0a. Reject any plugin directory that contains forbidden binary artifacts
    #     (.so, .pyc, .pyd, ELF binaries, etc.) that cannot be AST-scanned.
    #     This prevents native code injection via files that bypass the AST scanner.
    from agent.security.ast_safety import verify_artifact_safety
    plugin_files = {}
    for root, dirs, files in os.walk(plugin_path):
        # Skip __pycache__ (auto-generated .pyc files, not attacker-planted)
        dirs[:] = [d for d in dirs if d != '__pycache__']
        # Also reject dotfiles/dot-directories (excluded from signature hash)
        for d in dirs:
            if d.startswith('.') and d not in ('__pycache__',):
                raise RuntimeError(
                    f"Security violation: Plugin '{plugin_path.name}' contains dot-directory "
                    f"'{d}' which is excluded from signature verification. Remove it."
                )
        for f in files:
            if f.startswith('.'):
                raise RuntimeError(
                    f"Security violation: Plugin '{plugin_path.name}' contains dotfile "
                    f"'{f}' which is excluded from signature verification. Remove it."
                )
            fpath = Path(root) / f
            rel = fpath.relative_to(plugin_path)
            try:
                plugin_files[str(rel)] = fpath.read_bytes()
            except Exception:
                pass

    artifact_errors = verify_artifact_safety(plugin_files, base_description=f"plugin '{plugin_path.name}'")
    if artifact_errors:
        raise RuntimeError(
            f"Security violation in plugin '{plugin_path.name}': " + "; ".join(artifact_errors)
        )

    # 1. Check cryptographic signature
    sig_path = plugin_path / "signature.sig"
    is_signed = False
    if sig_path.exists():
        from agent.execution.tools.security import _verify_skill_signature
        _verify_skill_signature(plugin_path)
        is_signed = True
        print(f"[PLUGINS] Plugin '{plugin_path.name}' verified cryptographically.")

    # 2. AST safety scan on all Python files — always runs.
    #    For signed plugins: advisory only (log warnings, don't reject).
    #    For unsigned plugins: enforcement (raise on violation).
    from agent.security.ast_safety import verify_ast_safety
    scan_label = "advisory" if is_signed else "enforcement"
    print(f"[PLUGINS] AST scanning plugin '{plugin_path.name}' (mode: {scan_label})...")
    for rel_path, content in plugin_files.items():
        if rel_path.endswith('.py'):
            try:
                code = content.decode('utf-8', errors='replace')
                verify_ast_safety(code, rel_path)
            except Exception as e:
                if is_signed:
                    # Advisory only for signed plugins — log but don't block
                    print(f"[PLUGINS] AST advisory for signed plugin '{plugin_path.name}': {e}")
                else:
                    # Enforcement for unsigned plugins — reject
                    raise

    return plugin_files


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
        """Dynamically loads core integrations and web routes from the plugins directories.
        
        Plugin loading is disabled by default. Set enable_plugins=true in platform_config.json
        or ADA_ENABLE_PLUGINS=1 in the environment to opt in. Enabling plugins accepts that
        outside code will execute in-process with the host user's privileges.
        """
        from agent import tools
        from agent import memory

        # Plugin loading requires explicit opt-in via ADA_ENABLE_PLUGINS=1 in the host .env.
        # The .env file is read-only to containers, so the agent process cannot self-enable plugins.
        from agent.core.config import settings
        if not settings.ada_enable_plugins:
            print(
                "[PLUGINS] Plugin loading is DISABLED (default). "
                "To enable, set ADA_ENABLE_PLUGINS=1 in the host .env file. "
                "WARNING: Enabling plugins allows outside code to execute in-process. "
                "While reasonable precautions are taken (AST scanning, cryptographic signatures, "
                "binary artifact rejection), accepting use accepts potential risk. "
                "Security-in-depth should extend to host-level and network-level monitoring."
            )
            return

        # Save app reference for dynamic lazy loading
        self._app = app

        # Discover first
        self.discover_plugins()

        # Load dynamic platform configuration from unified settings
        disabled_plugins = settings.disabled_plugins
        lazy_plugins = getattr(settings, "lazy_plugins", ["playwright"])

        # Load pinned checksums from the read-only .env
        pinned_checksums = settings.parsed_plugin_checksums

        for name, plugin in self.plugins.items():
            if plugin.state == PluginState.ACTIVE:
                continue

            # Check if this plugin is explicitly disabled
            if name in disabled_plugins:
                print(f"[PLUGINS] Plugin '{name}' is disabled in configuration. Skipping.")
                continue

            # Verify pinned checksum from .env (if checksums are configured).
            # The operator pins approved checksums in the read-only .env file.
            # Even with a valid cryptographic signature, the plugin must match
            # the operator-approved checksum or it MUST NOT load.
            if pinned_checksums:
                expected_hex = pinned_checksums.get(name)
                if expected_hex is None:
                    print(
                        f"[PLUGINS] REFUSED plugin '{name}': no pinned checksum in "
                        f"ADA_APPROVED_PLUGIN_CHECKSUMS. Add '{name}:<sha256hex>' to .env."
                    )
                    plugin.state = PluginState.FAILED
                    plugin.error_message = "No pinned checksum in .env"
                    continue

                # Checksum will be verified AFTER loading files into memory
                # during AST safety verification (TOCTOU-safe: verify the same
                # bytes that were scanned, not a second disk read).
                pass  # Deferred to post-AST-scan verification below

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
                # Perform AST safety check — returns in-memory file contents
                # for TOCTOU-safe checksum verification
                verified_files = verify_plugin_ast_safety(plugin.path)

                # TOCTOU-safe checksum verification: compute hash from the same
                # in-memory bytes that were just scanned, not a second disk read.
                # Uses the same canonical path normalization as _calculate_skill_hash
                # and _calculate_in_memory_hash to ensure consistent digests.
                if pinned_checksums and name in pinned_checksums:
                    import hashlib
                    from agent.execution.tools.security import _canonical_rel_path
                    hasher = hashlib.sha256()
                    canonical_entries = []
                    for rel_path, content in verified_files.items():
                        canon = _canonical_rel_path(rel_path)
                        if canon != "signature.sig" and not Path(canon).name.startswith('.'):
                            canonical_entries.append((canon, content))
                    canonical_entries.sort(key=lambda e: e[0])
                    for rel_str, content in canonical_entries:
                        hasher.update(rel_str.encode('utf-8'))
                        hasher.update(content)
                    actual_hash = hasher.hexdigest()
                    expected_hex = pinned_checksums[name]
                    if actual_hash != expected_hex:
                        raise RuntimeError(
                            f"Checksum mismatch: expected {expected_hex}, got {actual_hash}. "
                            f"Update ADA_APPROVED_PLUGIN_CHECKSUMS in .env if this change is intentional."
                        )
                    print(f"[PLUGINS] Plugin '{name}' checksum verified (TOCTOU-safe): {actual_hash[:16]}...")

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

    def review_plugin(self, plugin_path: Path) -> dict:
        """Phase 1 of the two-phase plugin trust model: review a plugin without loading it.
        
        Runs the full verification chain in read-only mode:
        1. Binary artifact rejection (.so, .pyc, .pyd, ELF, PE, Mach-O)
        2. Dotfile/dot-directory rejection
        3. AST safety scan on all .py files
        4. Cryptographic signature verification (if signature.sig present)
        5. Computes SHA-256 checksum
        
        Returns a dict with:
        - 'name': plugin name
        - 'passed': True if all checks passed
        - 'checksum': SHA-256 hex digest of the plugin contents
        - 'has_signature': whether a signature.sig is present
        - 'signature_valid': whether the signature verified against trusted keys
        - 'scan_results': list of findings/issues
        - 'env_line': ready-to-paste .env line for ADA_APPROVED_PLUGIN_CHECKSUMS
        - 'files_scanned': number of files examined
        
        This method NEVER loads or executes the plugin. It only scans and reports.
        """
        results = {
            'name': plugin_path.name,
            'passed': False,
            'checksum': None,
            'has_signature': False,
            'signature_valid': False,
            'scan_results': [],
            'env_line': None,
            'files_scanned': 0,
        }
        
        # Step 1: Collect all files and check for binary artifacts and dotfiles
        from agent.security.ast_safety import verify_artifact_safety
        plugin_files = {}
        try:
            for root, dirs, files in os.walk(plugin_path):
                dirs[:] = [d for d in dirs if d != '__pycache__']
                for d in dirs:
                    if d.startswith('.') and d not in ('__pycache__',):
                        results['scan_results'].append(
                            f"FAIL: Dot-directory '{d}' found (excluded from signature hash)"
                        )
                        return results
                for f in files:
                    if f.startswith('.'):
                        results['scan_results'].append(
                            f"FAIL: Dotfile '{f}' found (excluded from signature hash)"
                        )
                        return results
                    fpath = Path(root) / f
                    rel = fpath.relative_to(plugin_path)
                    try:
                        plugin_files[str(rel)] = fpath.read_bytes()
                    except Exception:
                        pass
        except Exception as e:
            results['scan_results'].append(f"FAIL: Error walking plugin directory: {e}")
            return results
        
        results['files_scanned'] = len(plugin_files)
        
        # Step 2: Binary artifact check
        artifact_errors = verify_artifact_safety(
            plugin_files, base_description=f"plugin '{plugin_path.name}'"
        )
        if artifact_errors:
            for err in artifact_errors:
                results['scan_results'].append(f"FAIL: {err}")
            return results
        results['scan_results'].append("PASS: No forbidden binary artifacts detected")
        
        # Step 3: AST safety scan on all Python files
        from agent.security.ast_safety import verify_ast_safety
        ast_issues = []
        py_count = 0
        for rel_path, content in plugin_files.items():
            if rel_path.endswith('.py'):
                py_count += 1
                try:
                    code = content.decode('utf-8', errors='replace')
                    verify_ast_safety(code, rel_path)
                except Exception as e:
                    ast_issues.append(f"{rel_path}: {e}")
        
        if ast_issues:
            for issue in ast_issues:
                results['scan_results'].append(f"AST WARNING: {issue}")
            results['scan_results'].append(
                f"AST scan flagged {len(ast_issues)} issue(s) across {py_count} Python file(s). "
                "Review these carefully — the AST scanner is advisory, not exhaustive."
            )
        else:
            results['scan_results'].append(
                f"PASS: AST safety scan clean across {py_count} Python file(s)"
            )
        
        # Step 4: Cryptographic signature verification
        sig_path = plugin_path / "signature.sig"
        results['has_signature'] = sig_path.exists()
        if results['has_signature']:
            try:
                from agent.execution.tools.security import _verify_skill_signature
                _verify_skill_signature(plugin_path)
                results['signature_valid'] = True
                results['scan_results'].append("PASS: Cryptographic signature verified")
            except Exception as e:
                results['scan_results'].append(f"FAIL: Signature verification failed: {e}")
                return results
        else:
            results['scan_results'].append(
                "INFO: No signature.sig present. Plugin will require AST scan on every load."
            )
        
        # Step 5: Compute checksum (same algorithm as _calculate_skill_hash)
        from agent.execution.tools.security import _calculate_skill_hash
        checksum = _calculate_skill_hash(plugin_path).hex()
        results['checksum'] = checksum
        
        # Generate the .env-ready line
        results['env_line'] = f'{plugin_path.name}:{checksum}'
        
        # All checks passed
        results['passed'] = True
        results['scan_results'].append(
            f"PASS: Plugin '{plugin_path.name}' review complete. "
            f"Checksum: {checksum}"
        )
        
        return results

plugin_manager = PluginManager()
