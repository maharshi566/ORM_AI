"""The action agent's rules (documentation only: the action agent uses no model).

The action agent (app/agents/action.py) runs the actions a person approved, through the
action tools, and reports what each tool returned. There is nothing to decide there,
so no model is involved; this spec writes its rules down in the same shape as the
others.
"""

from app.prompts.base import PromptSpec

ACTION_PROMPT = PromptSpec(
    role="The action agent carries out approved actions with the write tools only.",
    goal="Do exactly what was approved, once, and report what each tool returned.",
    available_information="The approved actions, each with who approved it and their role.",
    constraints=(
        "- Run only actions with status approved or modified.\n"
        "- Pass the approval (who, role, approval ID) to the tool, which checks it again.\n"
        "- Use the action's idempotency key, so a retry replays instead of acting twice."
    ),
    output_schema="For each action: done with the tool's result, or failed with its reason.",
    failure_behavior="A failing action is reported as failed; the others still run.",
    grounding="An action is done only when its tool returned success.",
)
