import os
os.environ["ADA_DISABLE_SANDBOX"] = "1"
os.environ["TESTING"] = "1"
import sys
from pathlib import Path
project_root = str(Path(__file__).resolve().parent.parent)
abs_root = os.path.abspath(project_root)

new_path = []
for p in sys.path:
    if p:
        if os.path.abspath(p) == abs_root:
            continue
    else:
        if os.path.abspath(os.getcwd()) == abs_root:
            continue
    new_path.append(p)

sys.path = new_path
sys.path.append(project_root)
