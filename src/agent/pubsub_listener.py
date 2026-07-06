import sys
from agent.observability import pubsub_listener
sys.modules[__name__] = pubsub_listener
