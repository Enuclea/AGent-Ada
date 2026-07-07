"""AGent: CLI wrapper around the Google AntiGravity SDK."""
import sys

__version__ = "0.1.0"

# 1. Base storage & observability modules (no internal dependencies)
from agent.storage import db as db
from agent.observability import telemetry as telemetry
sys.modules['agent.db'] = db
sys.modules['agent.telemetry'] = telemetry

# 2. Keyless (depends on db)
from agent.core import keyless as keyless
sys.modules['agent.keyless'] = keyless

# 3. Memory & persistence package (depends on db)
from agent.storage import persistence as persistence
from agent.storage import conversation as conversation
import agent.memory as memory
from agent.memory import merge as merge
sys.modules['agent.persistence'] = persistence
sys.modules['agent.conversation'] = conversation
sys.modules['agent.memory'] = memory
sys.modules['agent.merge'] = merge

# 4. Observability shims (depend on telemetry/db)
from agent.observability import quiet_observer as quiet_observer
from agent.observability import grace_monitor as grace_monitor
from agent.observability import notifications as notifications
from agent.observability import pubsub_listener as pubsub_listener
sys.modules['agent.quiet_observer'] = quiet_observer
sys.modules['agent.grace_monitor'] = grace_monitor
sys.modules['agent.notifications'] = notifications
sys.modules['agent.pubsub_listener'] = pubsub_listener

# 5. Execution modules (depend on keyless/memory)
from agent.execution import tools as tools
from agent.execution import remote_worker as remote_worker
sys.modules['agent.tools'] = tools
sys.modules['agent.remote_worker'] = remote_worker

# 6. Core loop & Orchestrator (depends on tools/keyless/memory)
from agent.core import agent_loop as agent_loop
from agent.core import orchestrator as orchestrator
from agent.core import task_manager as task_manager
from agent.core import agent_types as agent_types
from agent.core import registry as registry
sys.modules['agent.agent_loop'] = agent_loop
sys.modules['agent.orchestrator'] = orchestrator
sys.modules['agent.task_manager'] = task_manager
sys.modules['agent.agent_types'] = agent_types
sys.modules['agent.registry'] = registry

# 7. Evaluation
from agent.evaluation import meta_evaluation as meta_evaluation
sys.modules['agent.meta_evaluation'] = meta_evaluation

# Interfaces sub-package
from agent.interfaces import cli as cli
from agent.interfaces import web as web

# Avoid registering sys.modules['agent.cli'] if we are running agent.cli as the main entry point
# to prevent loader collisions in runpy.
is_main_cli = False
frame = sys._getframe()
while frame:
    if frame.f_code.co_name in ('_run_module_as_main', '_get_module_details') and frame.f_locals.get('mod_name') == 'agent.cli':
        is_main_cli = True
        break
    frame = frame.f_back

if not is_main_cli:
    sys.modules['agent.cli'] = cli
sys.modules['agent.web'] = web
