"""Prompt for the response agent."""

from app.prompts.base import PromptSpec

RESPONSE_PROMPT = PromptSpec(
    role=(
        "You write a short, clear reply for a shopkeeper that separates facts, evidence, "
        "completed actions, pending approvals and sources."
    ),
)
