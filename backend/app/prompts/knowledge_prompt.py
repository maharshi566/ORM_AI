"""Prompt for the knowledge agent."""

from app.prompts.base import PromptSpec

KNOWLEDGE_PROMPT = PromptSpec(
    role=(
        "You find the shop policies and reference documents that apply to a request and "
        "cite each one."
    ),
)
