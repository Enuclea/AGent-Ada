import os
import sys
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from fastapi import FastAPI

from agent.interfaces.web import load_plugins
from agent.core.plugins import plugin_manager, PluginState

@pytest.fixture(autouse=True)
def clean_plugin_manager():
    plugin_manager.reset()
    yield
    plugin_manager.reset()

def test_plugin_loader_namespace_extensions():
    # Verify that agent.plugins module exists and has __path__
    import agent.plugins
    assert hasattr(agent.plugins, "__path__")
    assert len(agent.plugins.__path__) > 0

def test_plugin_loader_empty_or_invalid_paths():
    app = FastAPI()
    
    # If a path in plugin_paths does not exist, it should just be skipped without raising exceptions
    with patch("agent.plugins.__path__", ["/nonexistent/path/123", "/another/fake/path"]):
        # Should execute successfully without throwing errors
        load_plugins(app)

def sign_mock_plugin(plugin_path: Path):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    from agent.execution.tools.security import _calculate_skill_hash, add_test_trusted_key, _TEST_KEY_SENTINEL
    
    private_key = ed25519.Ed25519PrivateKey.generate()
    pub_key = private_key.public_key()
    pub_bytes = pub_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )
    pub_key_hex = pub_bytes.hex()
    
    # Register the public key securely in-process
    add_test_trusted_key(_TEST_KEY_SENTINEL, pub_key_hex)
    
    plugin_hash = _calculate_skill_hash(plugin_path)
    signature = private_key.sign(plugin_hash)
    (plugin_path / "signature.sig").write_bytes(signature)

def test_plugin_loader_mock_plugin_loading():
    app = FastAPI()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        plugin_dir = tmp_path / "mock_plugin"
        plugin_dir.mkdir()
        
        # Create a mock __init__.py file
        init_file = plugin_dir / "__init__.py"
        init_file.write_text("""
def setup_plugin(app, register_tools, register_scheduled_task):
    app.state.mock_plugin_loaded = True
""")
        sign_mock_plugin(plugin_dir)
        
        # Mock agent.plugins.__path__ to point to our temp directory
        with patch("agent.plugins.__path__", [str(tmp_path)]), \
             patch("agent.tools.register_plugin_tools", MagicMock()) as mock_register_tools, \
             patch("agent.memory.ensure_plugin_scheduled_task", MagicMock()) as mock_register_task:
            
            load_plugins(app)
            
            # Verify that the plugin setup was called and app state is updated
            assert getattr(app.state, "mock_plugin_loaded", False) is True

def test_plugin_lifecycle_states():
    app = FastAPI()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        plugin_dir = tmp_path / "mock_lifecycle_plugin"
        plugin_dir.mkdir()
        
        init_file = plugin_dir / "__init__.py"
        init_file.write_text("""
def setup_plugin(app, register_tools, register_scheduled_task):
    from agent.core.plugins import plugin_manager, PluginState
    app.state.mock_lifecycle_loaded = True
    app.state.state_during_setup = plugin_manager.plugins["mock_lifecycle_plugin"].state
""")
        sign_mock_plugin(plugin_dir)
        
        with patch("agent.plugins.__path__", [str(tmp_path)]), \
             patch("agent.tools.register_plugin_tools", MagicMock()), \
             patch("agent.memory.ensure_plugin_scheduled_task", MagicMock()):
            
            # Discover first
            plugin_manager.discover_plugins()
            assert "mock_lifecycle_plugin" in plugin_manager.plugins
            plugin = plugin_manager.plugins["mock_lifecycle_plugin"]
            assert plugin.state == PluginState.DISCOVERED
            
            # Load and verify state transitions
            plugin_manager.load_plugins(app)
            assert plugin.state == PluginState.ACTIVE
            assert getattr(app.state, "mock_lifecycle_loaded", False) is True
            assert getattr(app.state, "state_during_setup", None) == PluginState.LOADING

def test_plugin_failed_lifecycle_state():
    app = FastAPI()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        plugin_dir = tmp_path / "mock_fail_plugin"
        plugin_dir.mkdir()
        
        init_file = plugin_dir / "__init__.py"
        init_file.write_text("""
def setup_plugin(app, register_tools, register_scheduled_task):
    raise ValueError("Mock setup failure")
""")
        sign_mock_plugin(plugin_dir)
        
        with patch("agent.plugins.__path__", [str(tmp_path)]), \
             patch("agent.tools.register_plugin_tools", MagicMock()), \
             patch("agent.memory.ensure_plugin_scheduled_task", MagicMock()):
            
            # Load and verify state transitions to FAILED
            plugin_manager.load_plugins(app)
            assert "mock_fail_plugin" in plugin_manager.plugins
            plugin = plugin_manager.plugins["mock_fail_plugin"]
            assert plugin.state == PluginState.FAILED
            assert "Mock setup failure" in plugin.error_message

def test_sample_plugin_registration():
    # Skip if sample_plugin is not present in the external plugins folder
    plugins_dir = Path(__file__).resolve().parent.parent / "plugins"
    if not (plugins_dir / "sample_plugin").exists():
        pytest.skip("sample_plugin not present in external plugins folder")

    app = FastAPI()
    plugin_manager.reset()
    
    # Load all real plugins, which includes the new sample_plugin
    plugin_manager.load_plugins(app)
    
    # Check that sample_plugin is ACTIVE
    assert "sample_plugin" in plugin_manager.plugins
    assert plugin_manager.plugins["sample_plugin"].state == PluginState.ACTIVE
    
    # Verify the router endpoint works
    from fastapi.testclient import TestClient
    client = TestClient(app)
    response = client.get("/api/sample/test")
    assert response.status_code == 200
    assert response.json() == {"status": "success", "message": "hello from sample plugin"}

