import sys
from pathlib import Path

# Add project root to the end of sys.path to avoid shadowing third-party libraries (like 'discord')
project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)
