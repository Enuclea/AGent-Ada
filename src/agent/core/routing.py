"""Module managing the execution routing engine for the agent.

This module resolves which model provider/route (e.g. agy, grok, ollama, byok)
should be utilized to run a prompt based on availability, priority, and task priority.
"""

import os
import sys
import importlib
import inspect
import asyncio
from pathlib import Path
from typing import List, Optional, Type, Dict, Any, Tuple

from agent.routes.base import BaseRoute, RouteStatus, TaskPriority

_checking_routes = set()

async def check_primary_route_health(route_name: str, model: str):
    if route_name in _checking_routes:
        return
    _checking_routes.add(route_name)
    
    print(f"[HEALTH CHECK] Started periodic checks for primary route '{route_name}' using model '{model}'")
    try:
        # Check every 60 seconds
        for _ in range(30):  # limit to 30 attempts
            await asyncio.sleep(60)
            print(f"[HEALTH CHECK] Actively checking if primary route '{route_name}' is back up...")
            from agent.core.routing import routing_engine
            route = routing_engine.routes.get(route_name.lower())
            if not route:
                break
            
            try:
                # Run a simple check prompt
                res = await route.execute(
                    prompt="Hello, reply with exactly 'OK'",
                    model=model,
                    timeout=10.0
                )
                if res and "ok" in res.lower():
                    print(f"[HEALTH CHECK] Primary route '{route_name}' is BACK UP!")
                    try:
                        from agent.keyless import _circuit_breaker
                        _circuit_breaker.record_success(model)
                    except Exception:
                        pass
                    break
            except Exception as ce:
                print(f"[HEALTH CHECK] Route '{route_name}' still down: {ce}")
    finally:
        _checking_routes.discard(route_name)


class RoutingEngine:
    """Orchestrates model execution routing and fallback logic.

    Discovers built-in and user-custom routes, checks their priority and eligibility
    relative to model/task requirements, and runs prompt execution falling back across
    routes on failure.
    """

    def __init__(self, custom_routes_dir: Optional[str] = None) -> None:
        """Initializes RoutingEngine and registers all available routes.

        Args:
            custom_routes_dir: Optional custom directory path containing user routes.
        """
        self.routes: Dict[str, BaseRoute] = {}
        self.custom_routes_dir: str = custom_routes_dir or str(Path(__file__).parent.parent / "routes" / "custom")
        self._load_builtin_routes()
        self._load_custom_routes()

    def _load_builtin_routes(self) -> None:
        """Discovers and registers built-in core execution routes."""
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
        try:
            from agent.routes.byok import BYOKRoute
            self.register_route(BYOKRoute())
        except ImportError:
            pass

    def _load_custom_routes(self) -> None:
        """Dynamically loads custom user routes from the custom routes directory."""
        dir_path = Path(self.custom_routes_dir)
        if not dir_path.exists():
            return
        
        # Ensure parent routes package is in sys.path temporarily for imports
        sys_path_added = False
        package_root = str(dir_path.parent.parent.parent)
        if package_root not in sys.path:
            sys.path.append(package_root)
            sys_path_added = True

        for file in dir_path.glob("*.py"):
            # Exclude private modules and template files
            if file.name.startswith("_") or file.name.endswith(".example"):
                continue

            # --- Capability & Security Pre-check ---
            try:
                # 1. Verify owner & write permissions to prevent arbitrary write injections
                stat_info = file.stat()
                if stat_info.st_mode & 0o002:  # World-writable
                    print(f"[ROUTING] Security block: Refusing to load custom route {file.name} (file is world-writable)")
                    continue

                # 2. Search for dangerous system invocation patterns
                with open(file, "r", encoding="utf-8", errors="ignore") as f:
                    code_content = f.read()
                
                dangerous_signatures = ["eval(", "exec(", "os.system(", "subprocess.Popen(", "subprocess.run("]
                detected = [sig for sig in dangerous_signatures if sig in code_content]
                if detected:
                    print(f"[ROUTING] Security block: Refusing to load custom route {file.name} (detected dangerous signature(s): {detected})")
                    continue
            except Exception as se:
                print(f"[ROUTING] Security check failed for {file.name}: {se}")
                continue

            module_name = f"agent.routes.custom.{file.stem}"
            try:
                module = importlib.import_module(module_name)
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    # Register classes extending BaseRoute
                    if issubclass(obj, BaseRoute) and obj is not BaseRoute:
                        self.register_route(obj())
            except Exception as e:
                print(f"[ROUTING] Failed to load custom route {file.name}: {e}")

        # Clean up sys.path modification
        if sys_path_added:
            sys.path.remove(package_root)

    def register_route(self, route: BaseRoute) -> None:
        """Registers an execution route.

        Args:
            route: The BaseRoute instance to register.
        """
        self.routes[route.name.lower()] = route

    def get_route_status(self, route: BaseRoute) -> RouteStatus:
        """Determines the status of a route based on environment variables or defaults.

        Args:
            route: The route to check.

        Returns:
            The RouteStatus representing whether the route is PRIMARY, SECONDARY, etc.
        """
        env_key = f"ROUTE_{route.name.upper()}_STATUS"
        status_str = os.environ.get(env_key, route.default_status.value).lower()
        try:
            return RouteStatus(status_str)
        except ValueError:
            return route.default_status

    def get_route_priority(self, route: BaseRoute) -> int:
        """Determines the priority of a route based on environment variables or defaults.

        Args:
            route: The route to check.

        Returns:
            An integer representing execution order (lower is higher priority).
        """
        env_key = f"ROUTE_{route.name.upper()}_PRIORITY"
        try:
            return int(os.environ.get(env_key, str(route.default_priority)))
        except ValueError:
            return route.default_priority

    def resolve_routes(self, model: str, task_priority: TaskPriority = TaskPriority.INTERACTIVE) -> List[BaseRoute]:
        """Resolves and orders active routes that support the given model.

        Filters routes that match the target model and task requirements, sorting
        them by status (Primary first) and then priority order.

        Args:
            model: The target model name string.
            task_priority: The execution task priority category.

        Returns:
            A sorted list of eligible BaseRoute objects.
        """
        eligible_routes: List[Tuple[BaseRoute, RouteStatus]] = []
        
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
        def sort_key(item: Tuple[BaseRoute, RouteStatus]) -> Tuple[int, int]:
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
        """Runs execution sequence across eligible routes until one succeeds.

        Iteratively attempts to execute the prompt using resolved routes.
        If a route fails, logs the error and falls back to the next available route.

        Args:
            prompt: Text prompt input to execute.
            model: LLM model selection name.
            system_instructions: Optional context system instructions.
            timeout: Optional float execution timeout.
            conversation_id: Optional unique thread conversation ID.
            task_priority: Execution priority category.

        Returns:
            The textual completion response string.

        Raises:
            RuntimeError: If no routes support the model or if all eligible routes fail.
        """
        routes = self.resolve_routes(model, task_priority)
        if not routes:
            raise RuntimeError(f"No active routes support the model '{model}' for task priority {task_priority.name}")

        last_error = None
        for i, route in enumerate(routes):
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

                # Check if this was a primary route and we are about to failover to magica or onemin
                if route.name in ("agy", "byok") and i + 1 < len(routes):
                    next_route = routes[i + 1]
                    if next_route.name in ("magica", "onemin"):
                        # Determine cause of failure
                        err_msg = str(e).lower()
                        is_quota = any(k in err_msg for k in ("quota", "429", "rate limit", "rate_limit", "exhausted", "capacity"))
                        
                        if not is_quota:
                            # Connection/frontier model failure - notify user & schedule check
                            warning_msg = f"⚠️ Primary route '{route.name}' failed ({e}). Failing over to {next_route.name}..."
                            print(f"[ROUTING] {warning_msg}")
                            
                            # Log thought step in conversation if conversation_id is available
                            if conversation_id:
                                try:
                                    from agent.storage.conversation import log_conversation_step
                                    log_conversation_step(conversation_id, "thought", warning_msg)
                                except Exception as le:
                                    print(f"[ROUTING] Failed to log failover thought: {le}")
                            
                            # Spawn background health checker
                            asyncio.create_task(check_primary_route_health(route.name, model))

        raise RuntimeError(f"All execution routes failed. Last error: {last_error or 'No response'}")


# Global routing engine instance
routing_engine: RoutingEngine = RoutingEngine()
