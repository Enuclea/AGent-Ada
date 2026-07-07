import os
import shutil
import tempfile
from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock

from agent.core.routing import RoutingEngine
from agent.routes.base import BaseRoute, RouteInput, RouteOutput

class MockSafeRoute(BaseRoute):
    @property
    def name(self) -> str:
        return "SafeRoute"

    @property
    def default_status(self):
        return None

    @property
    def default_priority(self):
        return None

    @property
    def supported_models(self):
        return []

    async def execute(self, input_data: RouteInput) -> RouteOutput:
        return RouteOutput(response="Safe execution")

def test_custom_route_security_scanning():
    # Create temp directory for mock custom routes
    temp_dir = tempfile.mkdtemp()
    try:
        # 1. Create a safe custom route file
        safe_path = Path(temp_dir) / "safe_route.py"
        with open(safe_path, "w", encoding="utf-8") as f:
            f.write("""
# Safe route placeholder
""")

        # 2. Create a route containing a dangerous 'eval' call
        dangerous_path = Path(temp_dir) / "dangerous_route.py"
        with open(dangerous_path, "w", encoding="utf-8") as f:
            f.write("""
eval("import os")
""")

        # 3. Mock importlib.import_module
        mock_module = MagicMock()
        # Mock class inspection: inspect.getmembers returns list of (name, object)
        mock_module.SafeRoute = MockSafeRoute
        
        with patch("importlib.import_module", return_value=mock_module), \
             patch("inspect.getmembers", return_value=[("SafeRoute", MockSafeRoute)]):
             
            engine = RoutingEngine()
            engine.custom_routes_dir = temp_dir
            
            # Load routes
            engine._load_custom_routes()
            
            # Safe route should be imported and registered
            assert "saferoute" in engine.routes
            
            # Dangerous route should NOT be loaded/registered
            assert "dangerousroute" not in engine.routes
        
    finally:
        shutil.rmtree(temp_dir)
