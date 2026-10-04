"""Prompt for the validation agent."""

from app.prompts.base import PromptSpec

VALIDATION_PROMPT = PromptSpec(
    role=(
        "You check a draft reply against the evidence, citations and tool results, and "
        "return PASS, RETRY, HUMAN_REVIEW or BLOCK."
    ),
)
