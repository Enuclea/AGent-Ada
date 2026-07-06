import sys
from agent.execution import remote_worker
sys.modules[__name__] = remote_worker
