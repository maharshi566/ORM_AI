"""Conditional-edge functions.

Each function reads AgentState and returns the name of the next node, e.g.
"need more data" (max 2 loops), "needs approval", or the validator's
PASS / RETRY / HUMAN_REVIEW / BLOCK.
"""

# TODO(phase-4): routing functions with loop caps.
