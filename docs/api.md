# The ORM_AI API (Phases 6 and 7)

Every endpoint, what it needs and what it returns. The quickest way to try them is the
interactive page at <http://localhost:8000/docs> (start the backend first, see the
[README](../README.md#run-it-locally)). Each endpoint there has a **Try it out** button.

- [1. Logging in](#1-logging-in)
- [2. All endpoints](#2-all-endpoints)
- [3. Chat and the live progress stream](#3-chat-and-the-live-progress-stream)
- [4. Approvals](#4-approvals)
- [5. What the agents did: sessions, workflows, metrics](#5-what-the-agents-did-sessions-workflows-metrics)
- [6. Documents: upload and ingest](#6-documents-upload-and-ingest)
- [7. Errors](#7-errors)
- [8. Limits](#8-limits)
- [9. Settings](#9-settings)
- [10. How it is built](#10-how-it-is-built)

## 1. Logging in

A login is a **token**: a signed string that says who you are (user, shop, role) and
expires after `AUTH_TOKEN_HOURS` (12 by default). Send it with every request as the
header `Authorization: Bearer <token>`.

There are two modes, set in `.env`:

| `AUTH_REQUIRED` | Without a token | With a token |
| --- | --- | --- |
| `false` (local default) | Works. Say who you are in the body (`user_id`), as in Phases 4 and 5. Records of every shop can be read. | The token decides: its user and its shop. A different `user_id` or another shop is refused (403). |
| `true` (production) | 401 on every chat, approval, record, search and document call. | As above. |

**Getting a token on your own computer.** `POST /api/auth/dev-token` gives a token for any
demo user. It works only when `APP_ENV` is `local` or `test` and `AUTH_DEV_LOGIN=true`.
On any shared server it is refused, because it would let anyone log in as anyone. A real
deployment issues tokens from its own login system, signed with `AUTH_SECRET`.

Demo users (from the synthetic data):

| User | Shop | Role |
| --- | --- | --- |
| `USR-001` | SHOP-001 | owner |
| `USR-002` | SHOP-001 | staff |
| `USR-003` | SHOP-002 | owner |
| `USR-004` | SHOP-002 | staff |
| `USR-101` | none (every shop) | admin |

**In the /docs page:** open `POST /api/auth/dev-token`, click **Try it out**, send
`{"user_id": "USR-001"}`, copy `access_token` from the reply, click **Authorize** (top
right), paste it, and click **Authorize** again. Every call from that page now carries
the token. `GET /api/auth/me` shows who the token says you are.

**In PowerShell:**

```powershell
$login = Invoke-RestMethod -Method Post http://localhost:8000/api/auth/dev-token `
  -ContentType 'application/json' -Body '{"user_id": "USR-001"}'
$auth = @{ Authorization = "Bearer $($login.access_token)" }
Invoke-RestMethod http://localhost:8000/api/auth/me -Headers $auth
```

The token is checked on every call against the database: a user who was switched off
is logged out at once (401), and a user's current shop and role are used, not the ones
in an old token.

## 2. All endpoints

All paths start with `/api`. "Login" says what a token is needed for when
`AUTH_REQUIRED=true`. The rate-limit groups are explained in [8. Limits](#8-limits).

| Method | Path | What it does | Login | Rate limit |
| --- | --- | --- | --- | --- |
| GET | `/health` | 200 when PostgreSQL and Redis answer, 503 with details when one is down | no | – |
| POST | `/auth/dev-token` | A token for a demo user (local and test only) | no | login |
| GET | `/auth/me` | Who the token says you are (with your name and shop's name) | no | – |
| GET | `/auth/dev-users` | The demo users the development login accepts (local and test only) | no | login |
| POST | `/chat` | Ask about one shop; the answer with sources, actions, tool calls and agent steps | yes | agent |
| POST | `/chat/stream` | The same, with live progress as Server-Sent Events | yes | agent |
| GET | `/chat/graph` | The agent graph as a Mermaid diagram | no | – |
| POST | `/agent/run` | Run the agents on one task, without a conversation | yes | agent |
| POST | `/approval/{workflow_id}` | Approve, change or reject what a paused workflow waits on, then resume it | yes | agent |
| GET | `/approval/{workflow_id}` | What is waiting, and what was decided | yes | – |
| GET | `/sessions/{session_id}` | A conversation: its messages and workflows | yes | – |
| GET | `/sessions` | Your recent conversations (Phase 7) | yes | – |
| GET | `/workflows/{workflow_id}` | One workflow: every agent step, tool call and approval, and the reply as the chat showed it | yes | – |
| GET | `/workflows` | Recent requests, newest first; `?status=` filters (Phase 7) | yes | – |
| GET | `/approvals` | The approvals inbox; `?status=pending` for what waits (Phase 7) | yes | – |
| GET | `/shops` | The shops you can ask about (Phase 7) | yes | – |
| GET | `/metrics` | Counts and timings for the admin page | yes | – |
| GET | `/evaluations` | The latest evaluation runs, case by case (admins; Phase 7) | yes (admin) | – |
| GET | `/knowledge/search?q=…` | Search the documents; passages with citations ([rag.md](rag.md)) | yes | – |
| POST | `/documents/upload` | Add a .md, .txt or .pdf document | yes | upload |
| POST | `/documents/ingest` | Re-ingest the knowledge base and the uploads, in the background | yes (owner) | upload |
| GET | `/documents/ingest/{job_id}` | How an ingestion went | yes | – |

**Which shop can I see?** A logged-in owner or staff member sees only their own shop:
another shop's conversation, workflow or approval gives 403, and search and metrics are
limited to their shop. An admin sees every shop. Without a token (local mode only), every
shop can be read.

## 3. Chat and the live progress stream

`POST /api/chat`:

```json
{ "shop_id": "SHOP-001", "message": "How much does CUST-0001 owe?" }
```

The reply has `workflow_id`, `session_id` (send it back with the next message to keep
the conversation going), `status` (`completed`, `needs_clarification`,
`awaiting_approval`, `blocked` or `failed`), `answer`, `sources`, `proposed_actions`, `tool_calls`, `agents`
(each step with its time and tokens), `validation` and, when it paused, `approval`.
[agents.md](agents.md) explains each part.

`POST /api/chat/stream` takes the same body but answers with a stream of
**Server-Sent Events**: small messages sent one by one over the same connection, so the
frontend can show "Investigating…" while the agents work. Each event has a name and a
JSON body:

| Event | When | Data |
| --- | --- | --- |
| `stage` | an agent starts or finishes | `stage` (triage, routing, retrieval, investigation, approval, action, response, validation), `agent`, `state` (started / finished), and on finish `status`, `summary`, `latency_ms` |
| `approval_requested` | the workflow pauses for a person | the same `approval` block as in the chat reply |
| `result` | the end | exactly the JSON `POST /api/chat` returns |
| `error` | the run failed | `code`, `message`, `status` (the HTTP status `/api/chat` would have answered) |

The last event is always `result` or `error`. To watch one in PowerShell, put the
question in a file and send it with `curl.exe` (the real curl that comes with Windows;
plain `curl` in PowerShell is a different command). `-N` shows each event as it arrives:

```powershell
'{"shop_id": "SHOP-002", "message": "PO-00585 still has not arrived. What should I do?"}' | Set-Content body.json
curl.exe -N -X POST http://localhost:8000/api/chat/stream -H "Content-Type: application/json" -d "@body.json"
```

If the browser goes away mid-stream, the workflow still runs to the end, so an approved
action is never left half-reported. On shutdown the server gives running streams up to
30 seconds to finish.

`POST /api/agent/run` is for one-off tasks from scripts: `{"shop_id": "SHOP-001", "task":
"List products below their reorder level"}`. It runs the same agents and returns the same
shape, but starts no conversation and remembers nothing (`session_id` is empty).

## 4. Approvals

When a chat reply has `"status": "awaiting_approval"`, nothing has been changed yet. Its
`approval` block lists each waiting action with an `approval_id`, who must decide
(staff or owner), why, and the evidence and rules behind it.

`POST /api/approval/{workflow_id}`:

```json
{ "decision": "approve" }
{ "decision": "reject", "note": "Call the supplier first" }
{ "decisions": [ { "approval_id": "…", "decision": "modify",
                   "arguments": { "product_id": "PRD-0085", "new_selling_price": 358,
                                  "reason": "Owner set Rs 358." } } ] }
```

Logged in, the token says who decides. Without a token (local mode), add
`"user_id": "USR-003"`. The rules are checked again when the decision arrives, with the
records as they are now: staff cannot approve what needs the owner, and a change may not
switch to other records. The reply has the same shape as a chat reply and lists only what
a tool confirmed as done. An admin may enter a decision for a shop user (with that user's
`user_id`); the audit log records the admin as the one who entered it.

## 5. What the agents did: sessions, workflows, metrics

These only read. They feed the frontend's workflow panel and admin page (Phase 7).

- `GET /api/sessions/{session_id}`: the conversation's messages (user and assistant, in
  order) and a summary of each workflow it started.
- `GET /api/workflows/{workflow_id}`: status, the question and the final answer, every
  agent step (model, time, tokens, summary, error), every tool call (arguments, status,
  error code, time), the approvals, and, while it waits, the `approval` block. Its
  `reply` is the whole reply as `POST /api/chat` gave it (sources with their passages,
  actions and their results, agent steps), rebuilt from the saved graph state, so an old
  conversation can be shown exactly like a new one. It is empty while the workflow is
  still running, and when the saved state is gone (`CHECKPOINTER=memory` after a restart).
- `GET /api/metrics`: workflows by status (and in the last 24 hours), each agent's runs,
  errors, average and 95th-percentile time and tokens, each tool's calls and error codes,
  approvals by status, and documents and chunks. `scope` says whether it covers one shop
  or all.

**Lists for the website (Phase 7).** Each stays inside the caller's shop; an admin (or
local development without a token) sees every shop, or one with `?shop_id=SHOP-002`. A
shop user who asks for another shop gets 403.

- `GET /api/sessions?limit=20`: your recent conversations, newest first, each with how
  many requests it has and the status of the latest. A shop user sees the ones they
  started; an admin sees everyone's.
- `GET /api/workflows?status=awaiting_approval&limit=50`: recent requests, newest first.
- `GET /api/approvals?status=pending&limit=50`: the approvals inbox. Each item has the
  action (`tool`, `description`, `arguments`), why, who decides (`required_role`), the
  estimate, the rules, the request that led to it (`user_query`), and, once decided,
  `decided_by`, `decided_at` and `decision_note`. `counts` gives every status in the same
  scope (the website's badge).
- `GET /api/shops`: the shops you can ask about: yours, or all 50 for an admin.
- `GET /api/evaluations?limit=5`: the latest runs of `python -m scripts.eval_agent`
  (which saves each run in the `evaluations` table; `--no-save` skips that): per run the
  models, how many cases passed every check, and each score's average; per case its
  checks, time and details. Evaluation cases cover many shops, so only an admin sees them
  (403 for shop users).
- `GET /api/auth/dev-users`: the demo users with their shop names, for the login page.
  Like the development login, it works only with `APP_ENV` local or test.

## 6. Documents: upload and ingest

`POST /api/documents/upload` is a form upload (multipart), not JSON. Fields:

| Field | |
| --- | --- |
| `file` | The document: `.md`, `.txt` or `.pdf`, at most `UPLOAD_MAX_MB` (5 MB) and, for PDFs, 60 pages |
| `title` | Shown in citations. Default: the file name |
| `category` | `policy`, `sop`, `faq`, `supplier_terms`, `shop_profile` or `supplier_flyer` (default `sop`) |
| `shop_id` | Which shop it is for. Logged in, your own shop is used |
| `trusted` | `true` lets the agents rely on it like a shop rule. Only a logged-in owner (or admin) can set it; for anyone else the upload is saved as untrusted (reference only) and `notes` says so |
| `ingest` | `true` (default): start ingestion right away |

In the /docs page this endpoint shows a file picker. The checks, in order:

1. **Size.** A body over the limit is refused before it is read (413).
2. **Type.** The bytes must match the extension: a PDF must start like a PDF, and a text
   file must be UTF-8 with no binary content. A renamed program or archive is refused (415).
3. **Name.** The file is saved as `UPL-<shop>-<hash>`, never under the uploaded name, in
   `UPLOAD_DIR/<shop>/`. A name like `../../app/main.py` cannot write anywhere else.
4. **Metadata.** It is written by the server. The file's own front matter is thrown away,
   so an upload cannot pose as a shop policy (`document_id: POL-CREDIT-001`) or mark itself
   trusted.
5. **Usable.** The stored file is read back with the same loader ingestion uses. A PDF
   without text, or a file with only comments, is refused here (422), not at ingestion.

The same file uploaded again by the same shop (even as `.txt` instead of `.md`) is
recognised: `already_uploaded` is `true`, and the reply shows what is stored.

`POST /api/documents/ingest` (owner or admin) re-ingests the knowledge base and every
upload as a background task and answers 202 at once with a `job_id`. Only one ingestion
runs at a time (409 otherwise). `GET /api/documents/ingest/{job_id}` shows `status`
(`running`, `done`, `failed`) and a `summary`: documents, chunks, added, unchanged,
removed, and `skipped_uploads`. An upload that cannot be used is skipped and listed
there; it never stops ingestion for everyone else. Chunks that did not change are not
embedded again, so re-ingesting is cheap.

## 7. Errors

Every error has the same shape, and every response carries an `X-Request-ID` header that
matches the server's log lines:

```json
{ "error": { "code": "forbidden", "message": "You are logged in for SHOP-001, not SHOP-002.",
             "request_id": "4f1c…", "details": null } }
```

| Status | `code` | Meaning |
| --- | --- | --- |
| 401 | `unauthorized` | No token where one is needed, a broken or expired token, or a user who was switched off |
| 403 | `forbidden` | Another shop, another user, a role that may not do this, or development logins switched off |
| 404 | `not_found` | No such shop, user, conversation, workflow or job |
| 409 | `conflict` | Not possible right now: already decided, an ingestion already running |
| 413 | `request_too_large`, `file_too_large` | Too big (see below) |
| 415 | `unsupported_file` | Not a .md, .txt or .pdf, or the bytes do not match the extension |
| 422 | `validation_error`, `invalid_request` | The request does not have the right fields, or asks for something impossible |
| 429 | `rate_limited` | Too many requests; the `Retry-After` header says how many seconds to wait |
| 503 | `dependency_unavailable`, `model_unavailable` | The database, Redis, the knowledge store or the language model cannot be reached |
| 504 | `timeout` | It took too long and was stopped |
| 500 | `internal_error` | A bug. The message never includes internal details; the log has them |

## 8. Limits

| Limit | Default | Setting |
| --- | --- | --- |
| Chat, stream, agent run and approval calls per user (or per address without a token) | 20 a minute | `RATE_LIMIT_AGENT` |
| Development logins per address | 20 a minute (counted apart) | `RATE_LIMIT_AGENT` |
| Uploads and ingestion starts | 10 an hour | `RATE_LIMIT_UPLOAD` |
| Request body, uploads | 5 MB plus room for the form fields | `UPLOAD_MAX_MB` |
| Request body, everything else | 1 MB | – |
| Chat message | 2,000 characters | – |

Each model-backed call costs money or free-tier quota, which is what the agent limit
protects. The counts live in the backend's memory. With several backend processes, set
`RATE_LIMIT_STORAGE` to a Redis URL so they share one count. `RATE_LIMIT_ENABLED=false`
turns limits off (for load tests).

## 9. Settings

All in `.env`; [.env.example](../.env.example) has each one with an explanation.

| Setting | Local | Production |
| --- | --- | --- |
| `APP_ENV` | `local` | `production` |
| `AUTH_SECRET` | may stay empty (a new random one at each start, so tokens stop working after a restart) | **required**, at least 32 random characters |
| `AUTH_REQUIRED` | `false` | **must be** `true` |
| `AUTH_DEV_LOGIN` | `true` | ignored (always refused) |
| `CORS_ORIGINS` | `http://localhost:3000` | the frontend's address, never `*` |
| `UPLOAD_DIR` | `./data/uploads` | a disk that survives restarts (Docker: the `uploads_data` volume) |

With `APP_ENV=production` the backend **refuses to start** when `AUTH_SECRET`,
`AUTH_REQUIRED` or `CORS_ORIGINS` is unsafe, and the log says what to change. Make a
secret with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 10. How it is built

| Part | File |
| --- | --- |
| Tokens and `current_user` | `backend/app/core/auth.py` |
| Who a request acts for (token or `user_id`) | `backend/app/api/identity.py` |
| Rate limits (the `limits` library, behind SlowAPI) | `backend/app/core/rate_limit.py` |
| Body size limit, request IDs | `backend/app/core/middleware.py` |
| Error handlers | `backend/app/core/exceptions.py` |
| Routes | `backend/app/api/routes/` (one file per group) |
| Uploads and ingestion jobs | `backend/app/services/documents_service.py` |
| Sessions, workflows, metrics and the Phase 7 lists | `backend/app/services/records_service.py` |
| Tests (every endpoint over HTTP) | `backend/tests/test_api_phase6.py`, `test_api_phase7.py` |

Some choices, and why:

- **Tokens are standard JWTs (HS256), made with Python's own `hmac`.** No extra package,
  and any login system that can sign HS256 JWTs with `AUTH_SECRET` can issue them later.
- **Dependencies, not middleware, for logins and limits.** FastAPI runs them per route,
  so each route says what it needs, and `/docs` shows the Authorize button.
- **The stream uses plain Server-Sent Events.** One-way progress needs nothing more than
  WebSockets offer. Browsers support it, and it passes through proxies as ordinary HTTP.
- **Two requests can race for the next record number** (two new purchase orders at the
  same moment). On PostgreSQL the insert runs in a savepoint and the loser takes a later
  number. On SQLite (the light local demo) the loser gets a conflict it can retry.
  `backend/tests/test_tools_postgres.py` runs eight requests at once against a real
  PostgreSQL to check this.
