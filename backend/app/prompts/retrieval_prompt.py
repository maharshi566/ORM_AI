"""Prompt for the retrieval agent."""

from app.prompts.base import PromptSpec

RETRIEVAL_PROMPT = PromptSpec(
    role=("You fetch the shop records a request needs, using only the read tools you are given."),
)
