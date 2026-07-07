"""Agent plugins package.

Plugins extend the core agent with additional capabilities.
Each plugin subdirectory should contain a setup_plugin() function.
"""
import os
from pathlib import Path

# Dynamically extend namespace package path to load private/external plugins
# from the gitignored /plugins/ directory at the project root.
_external_plugins_dir = Path(__file__).resolve().parent.parent.parent.parent / "plugins"
if _external_plugins_dir.exists() and _external_plugins_dir.is_dir():
    __path__.append(str(_external_plugins_dir))
