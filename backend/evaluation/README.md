# evaluation/

Test sets and reports. Reports are generated and not committed.

- `datasets/retrieval_questions.yaml`: 20 questions plus 2 off-topic ones for the
  knowledge search (Phase 3 gate). Run `python -m scripts.eval_retrieval`.
- `datasets/agent_cases.yaml`: the 40 agent cases (Phases 4, 5 and 8), in seven
  categories: 10 normal, 5 approval, 5 ambiguous, 5 missing information, 5 tool failure,
  5 prompt injection and 5 policy conflict. Each states the expected intent, records,
  cited documents, and where it matters the route (answer, ask back, or wait for a
  person), the escalation (nobody, staff or owner), the tools that must run or fail, the
  guardrail verdict and an injected API fault. The header of the file explains every
  field. Run `python -m scripts.eval_agent --temperature 0 --pause 30` (needs a chat
  model; see docs/agents.md, section 10).
- `reports/`: Markdown reports written by those scripts: every rate against the 0.8
  gate, results by category, time per case (median and 95th percentile), tokens, and
  why each missed case missed.
- The `evaluations` table: `eval_agent` also saves each run there, one row per case
  (`--no-save` skips it). The website's admin page shows the latest runs, with the score
  per check and per category.

Adding a case: copy one of the same category, point it at records that exist in that
shop (`backend/data/seed/EDGE_CASES.md` lists the planted ones), and run
`pytest tests/test_agent_eval.py`. The dataset is checked when it loads (a misspelt
route, escalation, fault or category is an error, not a silent zero), and the scripted
model must pass every case, so the expectations stay achievable.
