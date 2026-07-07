import sys
from pathlib import Path
import pytest

# Add project root to sys.path to allow importing enuclea package
sys.path.append(str(Path(__file__).resolve().parent.parent))

def test_namespace_resolution_paths():
    """Verify that agent.plugins.__path__ contains the external plugins directory."""
    import agent.plugins
    
    paths = list(agent.plugins.__path__)
    assert len(paths) >= 2, f"Expected at least 2 plugin paths, got {paths}"
    
    # First path should be the built-in package directory
    builtin_path = Path(paths[0])
    assert builtin_path.name == "plugins"
    assert builtin_path.parent.name == "agent"
    
    # Second path should be the external plugins directory at the project root
    external_path = Path(paths[1])
    assert external_path.name == "plugins"
    assert external_path.parent.name == "AGent"
    assert external_path.exists()
    assert external_path.is_dir()

def test_namespace_resolution_imports():
    """Verify that external plugins can be imported successfully via the agent.plugins namespace."""
    import agent.plugins.enuclea_plugin as enuclea_plugin
    import agent.plugins.multi_tenant_plugin as multi_tenant_plugin
    
    assert enuclea_plugin is not None
    assert multi_tenant_plugin is not None
    assert hasattr(enuclea_plugin, "setup_plugin")
    assert hasattr(multi_tenant_plugin, "setup_plugin")

def test_namespace_resolution_nonexistent_fallback():
    """Verify that namespace resolution tolerates non-existent or empty built-in paths gracefully."""
    import agent.plugins
    
    # Store original __path__
    orig_path = list(agent.plugins.__path__)
    try:
        # Simulate empty/non-existent built-in path by putting a dummy path
        dummy_path = "/nonexistent/path/for/testing/plugins"
        agent.plugins.__path__ = [dummy_path] + orig_path[1:]
        
        # Verify that we can still resolve from the external directory
        import importlib
        spec = importlib.machinery.PathFinder.find_spec("agent.plugins.enuclea_plugin", agent.plugins.__path__)
        assert spec is not None
        assert spec.submodule_search_locations is not None
        assert any("plugins/enuclea_plugin" in p or "plugins/enuclea_plugin" in str(Path(p)) for p in spec.submodule_search_locations)
    finally:
        # Restore __path__
        agent.plugins.__path__ = orig_path
