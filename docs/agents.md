# How the agents work (Phase 4)

Phase 4 turns the tools (Phase 2) and the knowledge search (Phase 3) into an assistant
you can talk to: `POST /api/chat`. A message goes through a **graph of agents** built
with LangGraph. Each agent has one job, sees only what it needs, and hands its result
to the next through a shared **state**.

- [1. One question, start to finish](#1-one-question-start-to-finish)
- [2. The agents](#2-the-agents)
- [3. How the supervisor routes](#3-how-the-supervisor-routes)
- [4. How the answer stays honest](#4-how-the-answer-stays-honest)
- [5. When something fails](#5-when-something-fails)
- [6. Memory and checkpoints](#6-memory-and-checkpoints)
- [7. Choosing the model: OpenAI, OmniRoute or OpenRouter](#7-choosing-the-model-openai-omniroute-or-openrouter)
- [8. Try it](#8-try-it)
- [9. Measure it: the Phase 4 gate](#9-measure-it-the-phase-4-gate)
- [10. Tracing with LangSmith](#10-tracing-with-langsmith)
- [11. Design choices](#11-design-choices)

## 1. One question, start to finish

The shopkeeper of SHOP-002 asks: *"Purchase order PO-00585 still has not arrived. What
should I do?"*

```mermaid
sequenceDiagram
    participant U as Shopkeeper
    participant API as POST /api/chat
    participant T as Triage
    participant S as Supervisor
    participant D as Data retrieval
    participant K as Knowledge
    participant I as Investigation
    participant R as Response
    participant V as Validator
    U->>API: message + shop_id
    API->>T: classify (fast model)
    T-->>S: intent=supplier_issue, PO-00585
    S->>D: records needed
    D->>D: model picks get_purchase_orders; registry runs it
    D-->>S: PO-00585 is placed, 7 days late
    S->>K: rules needed
    K-->>S: [POL-SUPPLIER-001 v1 §3. Late deliveries], ...
    S->>I: a problem to work out
    I-->>S: late delivery; propose follow_up_supplier (not run)
    I->>R: via human review (Phase 5 pauses here)
    R->>V: draft reply (smart model)
    V-->>API: PASS: citations and IDs are real
    API-->>U: answer, sources, proposed action, agent steps
```

The full graph, drawn from the code, is in [agent-graph.md](agent-graph.md). In short:

```text
START → triage → supervisor ─┬─> data_retrieval ─> supervisor
                             ├─> knowledge ──────> supervisor
                             ├─> investigation ─┬─> data_retrieval   (need more data, max 2×)
                             │                  ├─> human_review ─> respond
                             │                  └─> respond
                             ├─> respond ─> validate ─┬─> respond   (RETRY, max 2×)
                             │                        └─> finalize ─> END
                             ├─> clarify ─> finalize
                             └─> finalize   (out of scope, or triage failed)
```

## 2. The agents

| Agent | Model | Tools | Returns | Code |
| --- | --- | --- | --- | --- |
| **Triage** | fast | none | `TriageResult`: intent, category, priority, entities (IDs, names, dates, amounts), missing information, whether to ask a question first, whether the user wants an action | `agents/triage.py` |
| **Supervisor** | none (rules) | none | the next step | `agents/supervisor.py` |
| **Data retrieval** | fast, tool calling | the 13 read tools | the records, each labelled by the call that returned it | `agents/retrieval.py` |
| **Knowledge** | none | `search_knowledge` | up to 6 policy passages with citations | `agents/knowledge.py` |
| **Investigation** | smart | none | `InvestigationResult`: findings, evidence, rules applied, recommended action, proposed actions, confidence, "need more data?" | `agents/investigation.py` |
| **Response** | smart | none | `FinalResponse`: answer, facts, rules, next steps, pending approvals, citations | `agents/response.py` |
| **Validator** | none (checks) | none | PASS, RETRY or HUMAN_REVIEW | `agents/validator.py` |
| **Action** | (Phase 5) | the 8 action tools | what each tool did | `agents/action.py` |

Every model answer is a **Pydantic object**, not free text: the model is given the
schema (`agents/schemas.py`) and its reply is validated. The prompts follow one fixed
structure (role, goal, available information, constraints, output schema, failure
behaviour, grounding) and live in `app/prompts/`.

The **fast** model (`LLM_MODEL_FAST`) does the quick, frequent work: triage and choosing
tools. The **smart** model (`LLM_MODEL_SMART`) does the thinking: the investigation and
the reply. The same model can do both.

## 3. How the supervisor routes

The supervisor is a set of rules, not a model call. It reads the triage result and
what has already run:

| Intent | Records | Rules | Investigation |
| --- | --- | --- | --- |
| `stock_status`, `sales_report` | yes | | |
| `reorder`, `customer_credit`, `payment_reminder`, `supplier_issue`, `stock_discrepancy`, `pricing`, `returns` | yes | yes | yes |
| `policy_question`, `general_help` | | yes | |
| `out_of_scope` | fixed polite reply, no tools | | |

Triage can **add** a specialist (`recommended_route`), never remove one. A request
where the user wants something done (`wants_action`) always gets an investigation. If
triage says a question must be asked first ("Send him a reminder", with no earlier
message saying who), the workflow ends with that question.

Two loops, each capped by `AGENT_MAX_LOOPS` (default 2), so a workflow always ends:

- **Need more data.** The investigation can ask for specific records ("stock movements
  for PRD-0002"); the data agent fetches them and the investigation looks again.
- **Rewrite.** The validator can send the reply back with a list of what to fix.

## 4. How the answer stays honest

The model never touches the database, and nothing it says is trusted until code has
checked it:

1. **Tools, not memory.** Records come only from tool calls, which are shop-scoped,
   validated and logged (Phase 2). The data agent's model *asks* for tools; it never
   writes a record's values itself.
2. **IDs are checked.** Triage keeps an ID only if it appears in the conversation. The
   investigation drops findings about IDs no tool returned. The validator rejects a
   reply that mentions an unknown ID.
3. **Citations are checked.** Only citations of passages actually retrieved survive,
   in the investigation and in the reply. A question about a rule must cite one.
4. **Proposing is not doing.** Proposed actions must pass the tool's own input
   validation and get an idempotency key tied to the workflow, but in Phase 4 they are
   never run. The reply lists them under *"Proposed, not done yet"*, and the validator
   rejects any sentence like "I have sent the reminder".
5. **Documents are data.** Passages arrive wrapped as untrusted reference data, and
   supplier flyers are searched only when the question is about an offer (Phase 3).

## 5. When something fails

| What fails | What happens |
| --- | --- |
| The model is down or the key is wrong, at triage | The reply says why (the same plain sentence `check_llm` would print); no records are touched |
| The model fails later | The workflow carries on with what it has; the reply says what could not be done, and lists the records fetched |
| A model that cannot call tools | The data agent runs a fixed plan from the triage entities instead (for example `get_customer_account` for each customer ID) and adds a warning |
| A reply that does not match the schema | Shown the problems and asked once more; then the agent fails clearly |
| A model that does not support strict JSON schemas | JSON mode with the schema in the prompt (remembered per model) |
| The main model is missing or overloaded | `LLM_MODEL_FALLBACK`, when set, is tried once |
| A tool fails (supplier API down, timeout) | Reads are retried with backoff; the error is reported in the reply and in `errors` |
| The knowledge base is not ingested | The agents answer from the records and say the documents are unavailable |
| Anything else inside an agent | Logged with its traceback; the reply says "internal error" without details |

## 6. Memory and checkpoints

- **Short-term memory** (`services/memory_service.py`): the last `SESSION_MEMORY_TURNS`
  messages of a conversation, so "what about him?" works. Redis keeps them for speed;
  the `messages` table in PostgreSQL is the durable copy, read when Redis is down or
  has forgotten (after `SESSION_TTL_HOURS`).
- **Workflow state** (`graph/checkpointer.py`): LangGraph saves the state after every
  step in three tables (`graph_checkpoints`, `graph_checkpoint_blobs`,
  `graph_checkpoint_writes`), under the workflow ID. Phase 5 uses this to pause for an
  approval and resume later, even after a restart. `CHECKPOINTER=memory` keeps it in the
  process instead.
- **Records of the work**: every request gets a `workflows` row; every agent step an
  `agent_runs` row (model, time, tokens, one-line summary); every tool call a
  `tool_calls` row. Look at them in Adminer ([database.md](database.md#look-inside-the-database)).

## 7. Choosing the model: OpenAI, OmniRoute or OpenRouter

The agents need a model that supports **tool calling**, and preferably **structured
output** (strict JSON schema). Pick **one** of these blocks for `.env` (in the project
folder, next to `.env.example`). Keys go only in `.env`, never into a chat or a commit.

**A. OpenAI directly (simplest, recommended to start).** `gpt-5.4-mini` supports both
features and costs USD 0.75 per million input tokens and 4.50 per million output
tokens (OpenAI's price list, October 2026). One chat uses roughly 15,000-20,000 input
tokens, so about 2-3 US cents.

```env
OPENAI_API_KEY=sk-...your key...
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL_FAST=gpt-5.4-mini
LLM_MODEL_SMART=gpt-5.4-mini
```

Later, try a stronger model for `LLM_MODEL_SMART` (any name `python -m scripts.check_llm
--list gpt` shows).

**B. OmniRoute** (your own gateway, see [omniroute.md](omniroute.md)):

```env
LLM_BASE_URL=http://localhost:20128/v1
LLM_API_KEY=...key from the OmniRoute dashboard...
LLM_MODEL_FAST=auto/fast
LLM_MODEL_SMART=auto/smart
```

**C. OpenRouter** (one key, many providers):

```env
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-...your OpenRouter key...
LLM_MODEL_FAST=openai/gpt-5.4-mini
LLM_MODEL_SMART=openai/gpt-5.4-mini
```

With B or C, embeddings would also go to the gateway. To keep them on OpenAI (so the
ChromaDB collection you already ingested stays valid), add:

```env
EMBEDDING_BASE_URL=https://api.openai.com/v1
```

The OpenAI key is then taken from `EMBEDDING_API_KEY`, or else `OPENAI_API_KEY`; the
gateway's key is never sent to OpenAI.

Then check, from `backend/`:

```powershell
python -m scripts.check_llm
```

`Chat`, `JSON schema` and `Tool calling` should say `ok` for both models. A `warn` on
JSON schema is fine (the agents fall back to JSON mode); a `warn` on tool calling means
that model is a poor choice for the data agent.

### The key is rejected

The first lines of `check_llm` say which key was used (only its start and last four
characters, for example `sk-proj-…a1b2`) and where it was read from:

- **"...environment variable, which wins over .env"**: an `OPENAI_API_KEY` is set in
  Windows itself (by System Properties, `setx` or an installer), and it beats `.env`.
  Remove it in PowerShell, then close and reopen IntelliJ, because its terminals keep the
  old value:

  ```powershell
  [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $null, "User")
  Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
  ```

  If it is still there after reopening, it was set for the whole computer: Start menu,
  "Edit the system environment variables", **Environment Variables**, and delete it under
  **System variables** (needs an administrator).
- **"OPENAI_API_KEY in .env"**: open <https://platform.openai.com/api-keys>. If none of
  your keys ends with the same four characters, the key was deleted, revoked, cut short
  when pasting, or belongs to another account. Create a new key and paste it after
  `OPENAI_API_KEY=` on one line, with no quotes or spaces. Save, and run `check_llm` again.
- **"accepted the API key but refused this request (HTTP 403)"**: the key works but is
  restricted. Edit it on the same page and set **Permissions** to **All**.

Optional settings: `LLM_MODEL_FALLBACK` (a second model, tried once if the first fails),
`LLM_REASONING_EFFORT` (for reasoning models such as gpt-5.4-mini: `low` or `medium`
think longer; empty uses the model's default), `LLM_STRUCTURED_OUTPUT` (`auto` is right
almost always).

## 8. Try it

Start the backend (`uvicorn app.main:app --reload` in `backend/`), open
<http://localhost:8000/docs>, choose **POST /api/chat**, click **Try it out** and send:

```json
{ "shop_id": "SHOP-001", "message": "How much does CUST-0001 owe? Can I give them more on credit?" }
```

Or from PowerShell:

```powershell
$body = @{ shop_id = "SHOP-001"; message = "Which products are running low?" } | ConvertTo-Json
Invoke-RestMethod -Uri http://localhost:8000/api/chat -Method Post -ContentType "application/json" -Body $body
```

The reply contains:

| Field | What it is |
| --- | --- |
| `answer` | the reply, in Markdown |
| `details` | the same, split into facts, rules, next steps, pending approvals |
| `sources` | each cited passage: citation, title, section, excerpt |
| `proposed_actions` | what ORM_AI would do, waiting for approval (Phase 5) |
| `tool_calls` | every tool call, with status and time |
| `agents` | every agent step: model, time, tokens, one-line summary |
| `session_id` | send it back with the next message to continue the conversation |

Good questions to try come from the planted edge cases
(`backend/data/seed/EDGE_CASES.md`): CUST-0001 is over the credit limit, CUST-0004 was
reminded two days ago, PO-00585 (SHOP-002) is 7 days late, PO-00586 (SHOP-003) arrived
short, PRD-0085 (SHOP-004) sells below cost.

`GET /api/chat/graph` returns the graph as Mermaid.

## 9. Measure it: the Phase 4 gate

```powershell
python -m scripts.eval_agent
```

Runs the 10 normal cases in `backend/evaluation/datasets/agent_cases.yaml` with your
model and prints, per case: the intent, whether the expected records were fetched,
whether the expected rule was cited, the validator's verdict, time and tokens. The
gate passes when at least 8 of 10 intents are right and at least 8 of 10 replies are
grounded and cited. A report is written to `backend/evaluation/reports/`. `--case A04`
runs one case and prints its whole answer. It only reads records; nothing is changed.

## 10. Tracing with LangSmith

Optional. Create a free account at <https://smith.langchain.com>, make an API key and set:

```env
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
```

Restart the backend. Every chat then appears in LangSmith as a tree: each agent, each
model call (prompt, reply, tokens) and each step's state. A trace contains the shop's
records, so keep tracing off for real shops unless the owner agrees. The structured
logs (`agent_run` and `tool_call` lines) are written either way.

## 11. Design choices

- **LangGraph** runs the workflow: a typed state, nodes, conditional edges, loop caps,
  checkpoints and (Phase 5) `interrupt()` for approvals. LangChain is present through
  `langchain-core`, the base LangGraph runs on (runnables, callbacks, tracing).
- **Our own model client instead of LangChain's ChatOpenAI.** `services/chat_model.py`
  is small, uses the same endpoint logic as embeddings (OpenAI, OmniRoute, OpenRouter),
  explains errors in plain sentences, downgrades to JSON mode per model, falls back to a
  second model, and counts tokens. With ChatOpenAI each of those would need its own
  workaround.
- **Our own checkpointer instead of langgraph-checkpoint-postgres.** That package needs
  the psycopg driver, which cannot run on the event loop Windows uses by default. Ours
  uses the app's SQLAlchemy + asyncpg engine, so it works on Windows, with Supabase,
  and on SQLite in the tests. The tests run the same scenarios on LangGraph's own
  in-memory saver and on ours and require identical results.
- **Rules where rules are enough.** The supervisor, the knowledge agent's searches and
  the validator are code: predictable, free and testable. Models do the parts that need
  language: understanding the request, choosing tools, reasoning over evidence,
  writing the reply.
- **Tests without a model.** `tests/fake_llm.py` is a scripted model with the same
  interface, so the whole graph runs in the test suite with real tools, real search and
  a real database. `tests/scripted_gateway.py` puts that script behind an
  OpenAI-compatible endpoint, so the real client and SDK are tested too, including a
  check that every schema is valid for OpenAI's strict mode.

Phase 5 adds the approval step (pause, show the evidence, approve, reject or modify,
resume), the Action agent that runs approved actions, and the policy gate and input
guardrails.
