import os
import sys
import importlib
import inspect
from pathlib import Path
from typing import List, Optional, Type, Dict, Any

from agent.routes.base import BaseRoute, RouteStatus, TaskPriority

class RoutingEngine:
    def __init__(self, custom_routes_dir: Optional[str] = None):
        self.routes: Dict[str, BaseRoute] = {}
        self.custom_routes_dir = custom_routes_dir or str(Path(__file__).parent.parent / "routes" / "custom")
        self._load_builtin_routes()
        self._load_custom_routes()

    def _load_builtin_routes(self):
        """Discovers and registers built-in core routes."""
        try:
            from agent.routes.agy import AgyRoute
            self.register_route(AgyRoute())
        except ImportError:
            pass
        try:
            from agent.routes.grok import GrokRoute
            self.register_route(GrokRoute())
        except ImportError:
            pass
        try:
            from agent.routes.ollama import OllamaRoute
            self.register_route(OllamaRoute())
        except ImportError:
            pass

    def _load_custom_routes(self):
        """Dynamically loads custom user routes from the custom routes directory."""
        dir_path = Path(self.custom_routes_dir)
        if not dir_path.exists():
            return
        
        # Ensure parent routes package is in sys.path
        sys_path_added = False
        package_root = str(dir_path.parent.parent.parent)
        if package_root not in sys.path:
            sys.path.append(package_root)
            sys_path_added = True

        for file in dir_path.glob("*.py"):
            if file.name.startswith("_") or file.name.endswith(".example"):
                continue
            module_name = f"agent.routes.custom.{file.stem}"
            try:
                module = importlib.import_module(module_name)
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, BaseRoute) and obj is not BaseRoute:
                        self.register_route(obj())
            except Exception as e:
                print(f"[ROUTING] Failed to load custom route {file.name}: {e}")

        if sys_path_added:
            sys.path.remove(package_root)

    def register_route(self, route: BaseRoute):
        """Registers an execution route."""
        self.routes[route.name.lower()] = route

    def get_route_status(self, route: BaseRoute) -> RouteStatus:
        """Determines the status of a route based on environment variables or defaults."""
        env_key = f"ROUTE_{route.name.upper()}_STATUS"
        status_str = os.environ.get(env_key, route.default_status.value).lower()
        try:
            return RouteStatus(status_str)
        except ValueError:
            return route.default_status

    def get_route_priority(self, route: BaseRoute) -> int:
        """Determines the priority of a route based on environment variables or defaults."""
        env_key = f"ROUTE_{route.name.upper()}_PRIORITY"
        try:
            return int(os.environ.get(env_key, str(route.default_priority)))
        except ValueError:
            return route.default_priority

    def resolve_routes(self, model: str, task_priority: TaskPriority = TaskPriority.INTERACTIVE) -> List[BaseRoute]:
        """Resolves and orders active routes that support the given model."""
        eligible_routes = []
        
        for route in self.routes.values():
            if not route.supports_model(model):
                continue
            
            status = self.get_route_status(route)
            if status == RouteStatus.OFF:
                continue
            
            # urgent_only routes are only eligible for interactive or critical tasks
            if status == RouteStatus.URGENT_ONLY and task_priority > TaskPriority.SCHEDULED_CRITICAL:
                continue
                
            eligible_routes.append((route, status))

        # Sort routes:
        # 1. Primary status first
        # 2. Then by configured priority (lower number = runs first)
        def sort_key(item):
            route, status = item
            is_primary = 0 if status == RouteStatus.PRIMARY else 1
            priority = self.get_route_priority(route)
            return (is_primary, priority)

        eligible_routes.sort(key=sort_key)
        return [r for r, _ in eligible_routes]

    async def execute(
        self,
        prompt: str,
        model: str,
        system_instructions: Optional[str] = None,
        timeout: Optional[float] = None,
        conversation_id: Optional[str] = None,
        task_priority: TaskPriority = TaskPriority.INTERACTIVE,
    ) -> str:
        """Runs execution sequence across eligible routes until one succeeds."""
        routes = self.resolve_routes(model, task_priority)
        if not routes:
            raise RuntimeError(f"No active routes support the model '{model}' for task priority {task_priority.name}")

        last_error = None
        for route in routes:
            try:
                print(f"[ROUTING] Attempting execution via route '{route.name}' for model '{model}'")
                res = await route.execute(
                    prompt=prompt,
                    model=model,
                    system_instructions=system_instructions,
                    timeout=timeout,
                    conversation_id=conversation_id
                )
                if res is not None:
                    return res
            except Exception as e:
                print(f"[ROUTING] Route '{route.name}' failed: {e}")
                last_error = e

        raise RuntimeError(f"All execution routes failed. Last error: {last_error or 'No response'}")

# Global routing engine instance
routing_engine = RoutingEngine()
