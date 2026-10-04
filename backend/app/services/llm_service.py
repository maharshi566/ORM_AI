"""LLM wrapper used by every agent.

Picks LLM_MODEL_FAST or LLM_MODEL_SMART, returns structured output parsed into
Pydantic models, applies timeouts and exponential backoff, falls back to a
second model, and records token usage.
"""

# TODO(phase-4): implement with langchain-openai and tenacity.
