"""Validation / guardrail agent.

Checks grounding, citations, tool-result consistency, required fields and
sensitive actions before a reply goes out. Returns PASS, RETRY, HUMAN_REVIEW
or BLOCK, and the graph routes on that decision.
"""

# TODO(phase-5): deterministic checks first, then an LLM groundedness judge.
