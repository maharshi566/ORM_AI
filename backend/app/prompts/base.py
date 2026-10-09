"""A fixed structure for every agent prompt (spec section 19).

Keeping prompts as data makes them easy to review, version and test.
"""

from dataclasses import dataclass

TODO = "Not written yet (see the agent's phase in the build plan)."


@dataclass(frozen=True)
class PromptSpec:
    role: str
    goal: str = TODO
    available_information: str = TODO
    constraints: str = TODO
    output_schema: str = TODO
    failure_behavior: str = TODO
    grounding: str = TODO

    def render(self) -> str:
        sections = (
            ("Role", self.role),
            ("Goal", self.goal),
            ("Available information", self.available_information),
            ("Constraints", self.constraints),
            ("Output schema", self.output_schema),
            ("Failure behaviour", self.failure_behavior),
            ("Grounding", self.grounding),
        )
        return "\n\n".join(f"## {title}\n{body.strip()}" for title, body in sections)
