"""LangGraph node functions.

Each node wraps one agent: it reads the fields it needs from AgentState and
returns only the fields it changes.
"""

# TODO(phase-4): one node per agent, plus the policy-gate and human-approval nodes.
