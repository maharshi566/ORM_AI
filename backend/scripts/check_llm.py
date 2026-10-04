"""Test the model endpoint: OpenAI, or a gateway such as OmniRoute.

Usage (from backend/, with the virtual environment active):

    python -m scripts.check_llm                    # test the models set in .env
    python -m scripts.check_llm --model auto/fast  # test one chat model, by name
    python -m scripts.check_llm --embedding-model openai/text-embedding-3-small
    python -m scripts.check_llm --list             # every model the endpoint offers
    python -m scripts.check_llm --list embed       # only names containing "embed"

It sends about ten tiny requests (a few hundred tokens in all) and prints one line
for each: can it be reached, does chat work, can the model answer in JSON, can it call
a tool, do embeddings work. Run it after changing LLM_BASE_URL, a key or a model name.

Exit code: 0 everything works (warnings allowed), 1 something failed, 2 nothing is
configured.
"""

import argparse
import asyncio

from app.config.settings import get_settings
from app.services.llm_check import format_report, list_model_ids, run_checks


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--model",
        action="append",
        help="chat model to test (repeat for several); default LLM_MODEL_FAST and LLM_MODEL_SMART",
    )
    parser.add_argument("--embedding-model", help="override EMBEDDING_MODEL")
    parser.add_argument(
        "--list",
        nargs="?",
        const="",
        metavar="TEXT",
        help="list the models the endpoint offers (only those containing TEXT) and stop",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    if args.list is not None:
        ids, problem = await list_model_ids(get_settings(), contains=args.list)
        if problem:
            print(problem)
            return 1
        print("\n".join(ids) if ids else "No model matches.")
        return 0
    report = await run_checks(
        get_settings(), models=args.model, embedding_model=args.embedding_model
    )
    print(format_report(report))
    return report.exit_code


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
