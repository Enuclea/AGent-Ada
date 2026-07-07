import os
import logging
from pathlib import Path
from contextvars import ContextVar

logger = logging.getLogger("agent.execution.tools")

yield_requested = ContextVar("yield_requested", default=False)

SKILLS_DIR = Path.home() / ".agent" / "skills"
WORKSPACE_SKILLS_DIR = Path(os.getcwd()) / ".agents" / "skills"
OPENCLAW_EXTS_DIR = Path.home() / ".openclaw" / "extensions"
OPENCLAW_SKILLS_DIR = Path.home() / ".openclaw" / "skills"
HERMES_SKILLS_DIR = Path.home() / ".hermes" / "skills"

# Third-party / plugin registered tools list
PLUGIN_TOOLS = []

def register_plugin_tools(tools_list: list) -> None:
    """Dynamically registers tool functions from plugins."""
    global PLUGIN_TOOLS
    for t in tools_list:
        if t not in PLUGIN_TOOLS:
            PLUGIN_TOOLS.append(t)
