"""Prompt for the triage agent."""

from app.prompts.base import PromptSpec

TRIAGE_PROMPT = PromptSpec(
    role=(
        "You classify a shopkeeper's request, extract the products, suppliers, customers, "
        "dates and amounts it mentions, and list anything missing."
    ),
)
