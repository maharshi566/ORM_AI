"""Supervisor agent.

Reads the shared AgentState and decides which specialist runs next. It enforces
the loop caps (max 2 retrieval loops, max 2 response retries) and ends the
workflow. It delegates and never answers the shopkeeper itself. Holds no tools.
"""

# TODO(phase-4): build the routing node and RouteDecision model.
