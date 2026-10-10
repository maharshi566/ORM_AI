# evaluation/

Test sets and reports. Reports are generated and not committed.

- `datasets/retrieval_questions.yaml`: 20 questions plus 2 off-topic ones for the
  knowledge search (Phase 3 gate). Run `python -m scripts.eval_retrieval`.
- `datasets/agent_cases.yaml`: the 10 normal cases (Phase 4 gate) and 5 approval
  cases (Phase 5): expected intent, records, cited documents, and for approval cases the
  action and who must decide. Run `python -m scripts.eval_agent` (needs a chat model; see
  docs/agents.md).
- `reports/`: Markdown reports written by those scripts.
- The `evaluations` table: `eval_agent` also saves each run there, one row per case
  (`--no-save` skips it). The website's admin page shows the latest runs.

Phase 8 grows the agent set to 40 cases: 10 normal, 5 ambiguous, 5 missing
information, 5 tool failure, 5 prompt injection, 5 policy conflict and 5 human
approval, each with the expected intent, tools, documents, route and escalation.
