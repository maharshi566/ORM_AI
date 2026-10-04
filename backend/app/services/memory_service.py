"""Conversation memory.

Short-term: the last few turns per session in Redis, with the Postgres
messages table as the durable copy. Workflow state lives in the LangGraph
checkpointer, not here.
"""

# TODO(phase-4): store and load session history.
