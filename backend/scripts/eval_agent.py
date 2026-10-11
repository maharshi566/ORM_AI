"""Run the 40 agent cases with your real model (the Phase 4, 5 and 8 gates).

Usage (from backend/, with PostgreSQL seeded and the knowledge base ingested):

    python -m scripts.eval_agent                        # all 40 cases
    python -m scripts.eval_agent --case H02             # one case, printing the full answer
    python -m scripts.eval_agent --category tool_failure    # one category (repeatable)
    python -m scripts.eval_agent --pause 30             # wait between cases (free tiers)
    python -m scripts.eval_agent --temperature 0        # repeatable answers

Needs LLM_MODEL_FAST / LLM_MODEL_SMART and a key or gateway in .env (check with
python -m scripts.check_llm first). Each case makes about 4-6 model calls; the whole set
takes five to fifteen minutes and, on a free tier, ``--pause 30`` keeps it under the
per-minute limit. Model names that move without notice (``...-latest``) are refused, so
every stored score belongs to one known model (``--allow-unpinned`` runs anyway).

Nothing is changed in the shop's records: when a case pauses for approval, the script
checks what is waiting and for whom, then **rejects** it, so no action ever runs (the
tests approve them on a copy of the database instead). A case with a ``fault`` runs with
mock supplier and messaging APIs that fail that way. Prints one line per case and why it
missed, writes a Markdown report to evaluation/reports/ and saves the scores in the
evaluations table, where the website's admin page shows them (``--no-save`` skips that).
Exits with 0 when the gate passes, 1 when it does not, and 2 when something is not set up.
"""

import argparse
import asyncio
import dataclasses
import re
import sys
import uuid

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.evaluation import (
    CATEGORIES,
    AgentCase,
    load_agent_cases,
    miss_reasons,
    run_cases,
    save_report,
    to_markdown,
    unpinned_models,
)
from app.agents.human_review import reject_all
from app.config.paths import BACKEND_DIR, backend_path
from app.config.settings import get_settings
from app.core.logging import configure_logging
from app.core.tracing import configure_tracing
from app.graph.deps import AgentDeps
from app.graph.workflow import RECURSION_LIMIT, compile_graph
from app.models.database import dispose_engine, get_session_factory, init_engine
from app.rag.embeddings import EmbeddingConfigError
from app.rag.factory import build_retriever
from app.services.chat_model import OpenAIChatModel, chat_model_problem
from app.tools.api_tools import FaultInjector, MockMessagingAPI, MockSupplierAPI, default_clients
from app.tools.base import business_now
from app.tools.registry import ToolRegistry

DATASET = BACKEND_DIR / "evaluation" / "datasets" / "agent_cases.yaml"
REPORTS = BACKEND_DIR / "evaluation" / "reports"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--case", action="append", help="run only this case ID (repeatable)")
    parser.add_argument(
        "--category", action="append", choices=CATEGORIES, help="run only this category"
    )
    parser.add_argument("--min", type=float, default=0.8, help="gate for every rate (0.8)")
    parser.add_argument("--dataset", default=str(DATASET), help="cases YAML file")
    parser.add_argument(
        "--pause",
        type=float,
        default=0.0,
        help="seconds to wait between cases, to stay under a free tier's per-minute limit",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="sampling temperature for this run (default: LLM_TEMPERATURE, else the "
        "provider's); 0 makes runs repeatable",
    )
    parser.add_argument(
        "--allow-unpinned",
        action="store_true",
        help="run even with model names that move without notice (...-latest)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="do not store the scores in the database (the admin page shows stored runs)",
    )
    return parser.parse_args(argv)


def select_cases(cases: list[AgentCase], args: argparse.Namespace) -> list[AgentCase]:
    if args.case:
        wanted = {c.upper() for c in args.case}
        cases = [c for c in cases if c.id in wanted]
    if args.category:
        cases = [c for c in cases if c.kind in set(args.category)]
    return cases


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.temperature is not None:
        settings = settings.model_copy(update={"llm_temperature": args.temperature})
    configure_logging("WARNING", json_logs=settings.log_json)
    configure_tracing(settings)
    problem = chat_model_problem(settings)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    floating = unpinned_models([settings.llm_model_fast, settings.llm_model_smart])
    if floating and not args.allow_unpinned:
        print(
            f"{', '.join(floating)} can change without notice, so scores from different runs "
            "would not be comparable. Set a versioned model name in .env, or pass "
            "--allow-unpinned.",
            file=sys.stderr,
        )
        return 2
    try:
        retriever = await asyncio.to_thread(build_retriever, settings)
    except EmbeddingConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    cases = select_cases(load_agent_cases(backend_path(args.dataset)), args)
    if not cases:
        print("No case matches --case / --category.", file=sys.stderr)
        return 2

    init_engine(settings.database_url, transaction_pooler=settings.db_transaction_pooler)
    llm = OpenAIChatModel(settings)
    deps = AgentDeps(
        settings=settings,
        llm=llm,
        registry=ToolRegistry(retry_backoff_seconds=settings.tool_retry_backoff_seconds),
        session_factory=get_session_factory(),
        now=business_now(settings.business_date),
        knowledge=retriever,
        clients=default_clients(settings),
        record_to_db=False,  # an evaluation leaves no workflow rows behind
        run_actions=False,  # and never acts, not even on low-risk drafts or cases
    )
    graph = compile_graph(InMemorySaver())

    def deps_for(case: AgentCase) -> AgentDeps:
        """The case's fault, if any, in fresh mock APIs; everything else shared."""
        if not case.fault:
            return deps
        faults = FaultInjector(case.fault)
        clients = {
            "supplier_api": MockSupplierAPI(faults),
            "messaging_api": MockMessagingAPI(faults),
        }
        return dataclasses.replace(deps, clients=clients)

    async def run_one(case: AgentCase) -> dict:
        workflow_id = f"eval-{uuid.uuid4()}"
        state = {
            "workflow_id": workflow_id,
            "session_id": "evaluation",
            "user_id": None,
            "shop_id": case.shop_id,
            "user_query": case.message,
            "conversation_history": [],
        }
        config = {"configurable": {"thread_id": workflow_id}, "recursion_limit": RECURSION_LIMIT}
        print(f"{case.id} [{case.kind}] {case.message[:64]} ...", flush=True)
        context = deps_for(case)
        result = await graph.ainvoke(state, config=config, context=context)
        for item in result.get("__interrupt__") or []:
            decision = reject_all(item.value, decided_by="evaluation", note="evaluation run")
            return await graph.ainvoke(Command(resume=decision), config=config, context=context)
        return result

    temperature = settings.llm_temperature
    if temperature is None:
        temperature = "provider default"
    models = (
        f"fast={llm.model_for('fast')}, smart={llm.model_for('smart')}, temperature={temperature}"
    )
    print(f"Running {len(cases)} case(s) with {models} at {llm.endpoint.where}\n")
    saved = ""
    try:
        report = await run_cases(cases, run_one, pause_s=args.pause)
        report.models = models
        if not args.no_save:
            try:
                run_id = await save_report(get_session_factory(), report)
                saved = f"Saved as evaluation run {run_id} (the admin page shows it)."
            except Exception as exc:  # the report file still has everything
                saved = (
                    f"Could not save the scores in the database ({type(exc).__name__}); "
                    "the report file has them."
                )
    finally:
        await dispose_engine()

    print()
    for o in report.outcomes:
        tokens = o.input_tokens + o.output_tokens
        print(
            f"{o.case.id}: {'pass' if o.passed else 'MISS'} "
            f"(route {o.route_taken}, {o.latency_ms / 1000:.1f}s, {tokens} tokens)"
        )
        for reason in miss_reasons(o):
            print(f"    {reason}")
        for error in o.errors[:2]:
            print(f"    error: {error}")
        if o.case.is_approval_case and not o.approval_ok and o.investigation:
            print(f"    investigation: {o.investigation}")
        for warning in [w for w in o.warnings if "proposed" in w][:2]:
            print(f"    warning: {warning}")
        if args.case:
            for warning in o.warnings:
                print(f"    warning: {warning}")
            print(f"\n{o.answer}\n")

    print(
        "\nBy category: "
        + ", ".join(
            f"{name.replace('_', ' ')} {passed}/{total}"
            for name, total, passed in report.by_category()
        )
    )
    print(", ".join(f"{name} {rate:.2f}" for name, rate in report.rates().items()))
    verdict = "PASS" if report.passed(args.min) else "FAIL"
    print(f"Gate {args.min}: {verdict}")

    slug = re.sub(r"[^A-Za-z0-9.-]+", "-", llm.model_for("smart")).strip("-")
    path = REPORTS / f"agent-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_markdown(report, minimum=args.min), encoding="utf-8", newline="\n")
    print(f"Report: {path.relative_to(BACKEND_DIR) if path.is_relative_to(BACKEND_DIR) else path}")
    if saved:
        print(saved)
    return 0 if report.passed(args.min) else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
