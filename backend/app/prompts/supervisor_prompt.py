"""The supervisor's rules, written down.

The supervisor routes by rules in code (app/agents/supervisor.py), not by an LLM call:
routing must be predictable, cheap and testable, and the judgement it needs has already
been made by the triage agent. This spec documents those rules for readers and for the
admin page; it is not sent to a model.
"""

from app.prompts.base import PromptSpec

SUPERVISOR_PROMPT = PromptSpec(
    role=(
        "The supervisor coordinates ORM_AI's specialist agents for a local shop. It decides "
        "which agent runs next; it never answers the shopkeeper itself."
    ),
    goal="Send each request through the fewest specialists that can answer it well.",
    available_information=(
        "The triage result (intent, entities, recommended route, clarification), which "
        "specialists have already run, and any errors."
    ),
    constraints=(
        "- Out-of-scope requests get a polite fixed reply without tools.\n"
        "- A request that needs clarification gets the clarifying question.\n"
        "- Records first (data retrieval), then rules (knowledge), as the intent needs.\n"
        "- Problems and requests for action go to the investigation agent; plain look-ups "
        "and policy questions go straight to the response agent.\n"
        "- Loops are capped (AGENT_MAX_LOOPS) so a workflow always ends."
    ),
    output_schema=(
        "The next step: data_retrieval, knowledge, investigation, respond, clarify or finalize"
    ),
    failure_behavior="If triage failed, finish with an explanation of what went wrong.",
    grounding="Routing never invents facts; it only decides who looks them up.",
)
