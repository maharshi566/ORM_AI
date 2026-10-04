# evaluation/

The evaluation suite is built in Phase 8.

- `datasets/`: 40 cases in JSONL (10 normal, 5 ambiguous, 5 missing-information,
  5 tool-failure, 5 prompt-injection, 5 policy-conflict, 5 human-approval). Each
  case lists the expected intent, tools, documents, route and escalation.
- `reports/`: generated evaluation reports.
