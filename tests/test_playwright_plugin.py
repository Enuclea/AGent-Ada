import json
import tempfile
from pathlib import Path
from unittest import mock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.core.plugins import plugin_manager
from agent.plugins.playwright.routes import router

def test_playwright_plugin_discovery_and_metadata():
    # Discover plugins
    plugin_manager.reset()
    plugins = plugin_manager.discover_plugins()
    
    assert "playwright" in plugins
    plugin = plugins["playwright"]
    
    # Verify manifest.json metadata loading
    assert plugin.metadata.get("id") == "playwright"
    assert plugin.metadata.get("name") == "Playwright Automation"
    assert "playwright_browse_url" in [t["name"] for t in plugin.metadata.get("tools", [])]

def test_playwright_plugin_setup():
    app = FastAPI()
    mock_register_tools = mock.Mock()
    mock_register_task = mock.Mock()
    
    # Import and setup
    from agent.plugins.playwright import setup_plugin
    setup_plugin(app, mock_register_tools, mock_register_task)
    
    # Check that tools are registered
    mock_register_tools.assert_called_once()
    registered_tools = mock_register_tools.call_args[0][0]
    assert any(t.__name__ == "playwright_browse_url" for t in registered_tools)

def test_playwright_plugin_screenshot_endpoint():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    
    # Create a mock screenshot file
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(b"mock_png_data")
        tmp_path = Path(tmp.name)
        
    filename = tmp_path.name
    
    # Patch SCREENSHOTS_DIR to the temporary directory
    with mock.patch("agent.plugins.playwright.routes.SCREENSHOTS_DIR", tmp_path.parent):
        # 1. Successful retrieval
        resp = client.get(f"/api/playwright/screenshot/{filename}")
        assert resp.status_code == 200
        assert resp.content == b"mock_png_data"
        
        # 2. Not found
        resp_404 = client.get("/api/playwright/screenshot/nonexistent.png")
        assert resp_404.status_code == 404
        
        # 3. Path traversal protection
        resp_traversal = client.get("/api/playwright/screenshot/../../etc/passwd")
        assert resp_traversal.status_code == 404
        
    # Cleanup
    tmp_path.unlink()
