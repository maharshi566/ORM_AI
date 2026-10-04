"""Prompt for the investigation agent."""

from app.prompts.base import PromptSpec

INVESTIGATION_PROMPT = PromptSpec(
    role=(
        "You combine shop records and policies into findings with evidence, a recommended "
        "action and a confidence score."
    ),
)
