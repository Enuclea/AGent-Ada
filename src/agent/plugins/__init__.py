"""Agent plugins package.

Plugins extend the core agent with additional capabilities.
"""
import sys
from pathlib import Path

# Dynamically include external plugins folder in the package search path.
# This keeps the core package namespace matching the public repository.
_root = Path(__file__).resolve().parent.parent.parent.parent
_external_plugins = _root / "plugins"
if _external_plugins.exists() and _external_plugins.is_dir():
    __path__.append(str(_external_plugins))
