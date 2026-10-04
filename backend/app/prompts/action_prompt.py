"""Prompt for the action agent."""

from app.prompts.base import PromptSpec

ACTION_PROMPT = PromptSpec(
    role=(
        "You carry out approved actions using only the write tools you are given, and "
        "report exactly what each tool returned."
    ),
)
