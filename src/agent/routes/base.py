from abc import ABC, abstractmethod
from enum import Enum, IntEnum
import os
import shutil
import logging
from pathlib import Path
from typing import List, Optional

def get_harness_path() -> Optional[str]:
    """Resolves the path to the system-wide agy binary."""
    if "ANTIGRAVITY_HARNESS_PATH" in os.environ:
        return os.environ["ANTIGRAVITY_HARNESS_PATH"]
    if os.path.exists("/.dockerenv"):
        return None
    
    # Check system PATH for 'agy'
    agy_path = shutil.which("agy")
    if agy_path:
        try:
            cwd_path = Path.cwd().resolve()
            resolved_path = Path(agy_path).resolve()
            
            is_in_cwd = cwd_path in resolved_path.parents or resolved_path == cwd_path
            
            trusted_dirs = [
                Path("/usr/bin").resolve(),
                Path("/usr/local/bin").resolve(),
                Path("/bin").resolve(),
                Path("/sbin").resolve(),
                Path("~/.local/bin").expanduser().resolve(),
                Path("~/.gemini/antigravity-cli/bin").expanduser().resolve(),
            ]
            is_trusted_parent = resolved_path.parent in trusted_dirs
            
            if is_in_cwd or not is_trusted_parent:
                logging.warning(
                    f"agy binary path {agy_path} (resolved to {resolved_path}) failed security check. "
                    f"is_in_cwd={is_in_cwd}, is_trusted_parent={is_trusted_parent}."
                )
            else:
                return agy_path
        except Exception as e:
            logging.warning(f"Error resolving agy path: {e}")
        
    # Fallback to standard local bin paths
    user_home = Path.home()
    fallback_path = user_home / ".local" / "bin" / "agy"
    if fallback_path.exists() and fallback_path.is_file():
        return str(fallback_path)
            
    return None

def setup_keyless_environment() -> None:
    """Sets the ANTIGRAVITY_HARNESS_PATH env var if system agy is found."""
    harness_path = get_harness_path()
    if harness_path:
        os.environ["ANTIGRAVITY_HARNESS_PATH"] = harness_path

class TaskPriority(IntEnum):
    """Controls failover depth per call type to optimize quota usage."""
    INTERACTIVE = 0       # User is waiting — full failover chain including Grok
    SCHEDULED_CRITICAL = 1  # Grace monitor, Meta-Eval — Gemini + 3P, no Grok
    SCHEDULED_ROUTINE = 2   # Gmail check, Morgen sync — Gemini only, retry next cycle
    BACKGROUND = 3           # Compaction, observer — cheapest model, no failover

class RouteStatus(str, Enum):
    ON = "on"
    OFF = "off"
    PRIMARY = "primary"
    SECONDARY = "secondary"
    URGENT_ONLY = "urgent_only"

class BaseRoute(ABC):
    """Abstract base class for all execution routes in the AGent-Ada system."""

    @property
    @abstractmethod
    def name(self) -> str:
        """The unique name of the route (e.g., 'agy', 'grok', 'ollama', 'magica')."""
        pass

    @property
    @abstractmethod
    def default_status(self) -> RouteStatus:
        """The default status of the route if not configured."""
        pass

    @property
    @abstractmethod
    def default_priority(self) -> int:
        """The default execution priority (lower = run earlier)."""
        pass

    @property
    @abstractmethod
    def supported_models(self) -> List[str]:
        """List of model identifiers supported by this route (e.g. ['gemini-2.5-flash', '*'])."""
        pass

    @abstractmethod
    async def execute(
        self,
        prompt: str,
        model: str,
        system_instructions: Optional[str] = None,
        timeout: Optional[float] = None,
        conversation_id: Optional[str] = None,
    ) -> Optional[str]:
        """Executes the prompt on this route.

        Returns the text response string if successful, or None if it fails.
        """
        pass

    def supports_model(self, model: str) -> bool:
        """Returns True if this route supports the given model name."""
        if self.name.lower() == "agy" and get_harness_path() is None:
            return False
        model_lower = model.lower()
        # Ollama models should only go to the Ollama route
        if model_lower.startswith("ollama/") and self.name.lower() != "ollama":
            return False

        # Non-ollama models should not go to the Ollama route
        if not model_lower.startswith("ollama/") and self.name.lower() == "ollama":
            return False

        supported = [m.lower() for m in self.supported_models]
        if "*" in supported:
            return True
        
        # Check prefix match (e.g., 'gemini' matches 'gemini-2.5-flash')
        for s in supported:
            if model_lower.startswith(s) or s.startswith(model_lower):
                return True
        return False
