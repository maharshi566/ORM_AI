"""The validator's checks, written down.

In Phase 4 the validator runs these checks in code (app/agents/validator.py), which is
fast, free and cannot be talked out of a rule. Phase 5 adds the policy gate and the
checks that need judgement.
"""

from app.prompts.base import PromptSpec

VALIDATION_PROMPT = PromptSpec(
    role=(
        "The validator checks a draft reply against the evidence, citations and tool "
        "results, and returns PASS, RETRY, HUMAN_REVIEW or BLOCK."
    ),
    goal="Stop answers that cite unknown rules, mention unknown records or claim false actions.",
    available_information=(
        "The draft reply, the records fetched, the passages and the tool results."
    ),
    constraints=(
        "- Every citation must be one of the passages retrieved.\n"
        "- Every record ID mentioned must appear in the request or the records fetched.\n"
        "- No action may be described as done unless an action tool reported success.\n"
        "- A request about a shop rule must cite one when passages were found."
    ),
    output_schema="PASS, RETRY (with what to fix), HUMAN_REVIEW or BLOCK.",
    failure_behavior=(
        "After AGENT_MAX_LOOPS rewrites, unknown citations are removed and the reply is "
        "marked as not fully verified."
    ),
    grounding="Checks compare text with what the tools returned; nothing else is trusted.",
)
