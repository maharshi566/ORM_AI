"""Draw the agent graph as a Mermaid diagram, straight from the code.

Usage (from backend/):

    python -m scripts.draw_graph           # print the diagram
    python -m scripts.draw_graph --write   # update docs/agent-graph.md
    python -m scripts.draw_graph --check   # exit 1 if docs/agent-graph.md is out of date

GitHub, VS Code and IntelliJ show the Mermaid block in docs/agent-graph.md as a picture.
"""

import argparse
import sys

from app.config.paths import BACKEND_DIR
from app.graph.workflow import draw_mermaid

DOC = BACKEND_DIR.parent / "docs" / "agent-graph.md"

HEADER = """# The agent graph

Drawn from the code by `python -m scripts.draw_graph --write`; do not edit by hand.
Solid arrows always happen; dotted arrows are decisions (conditional edges). The
supervisor is the hub that every specialist reports back to. See
[agents.md](agents.md) for what each node does.
"""


def render() -> str:
    return f"{HEADER}\n```mermaid\n{draw_mermaid().strip()}\n```\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", action="store_true", help="update docs/agent-graph.md")
    parser.add_argument("--check", action="store_true", help="fail if the doc is out of date")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = DOC.read_text(encoding="utf-8") if DOC.exists() else ""
        if current != text:
            print(f"{DOC} is out of date: run python -m scripts.draw_graph --write")
            return 1
        print("docs/agent-graph.md is up to date.")
        return 0
    if args.write:
        DOC.write_text(text, encoding="utf-8", newline="\n")
        print(f"Wrote {DOC}")
        return 0
    sys.stdout.write(draw_mermaid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
