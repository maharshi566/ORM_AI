# ORM_AI website (Phase 7)

The website shopkeepers and the demo audience use: Next.js 16 (App Router), TypeScript and
Tailwind CSS 4. It talks only to the FastAPI backend at `NEXT_PUBLIC_API_URL`; no AI or
database key ever reaches the browser.

- [Run it](#run-it)
- [The pages](#the-pages)
- [Try the full approval example](#try-the-full-approval-example)
- [How it talks to the backend](#how-it-talks-to-the-backend)
- [Layout of the code](#layout-of-the-code)
- [The look](#the-look)
- [Troubleshooting](#troubleshooting)

## Run it

You need **Node.js 22.18 or newer** (24 LTS recommended). Check with `node --version`. If it
says `v16` or `v20`, install the current LTS in PowerShell, then close every Cursor window and
open it again so the new version is found:

```powershell
winget install OpenJS.NodeJS.LTS
node --version
```

Start the backend first (see the main [README](../README.md#run-it-locally)), then, in a
second terminal (click **+** in Cursor's terminal panel):

```powershell
cd frontend
Copy-Item .env.example .env.local   # once; it says where the backend is
npm install                         # once, and after each git pull that changes package.json
npm run dev                         # http://localhost:3000
```

| Command | What it does |
| --- | --- |
| `npm run dev` | Development server with hot reload |
| `npm run lint` | ESLint |
| `npm run typecheck` | TypeScript type check |
| `npm test` | The website's own logic: the live-progress reader, the conversation model, login redirects (Node's built-in test runner) |
| `npm run build` | Production build (what Vercel runs) |

`npm install` reports a few "high severity" advisories in ESLint's own dependencies. They
affect lint tooling only, not the app. Don't run `npm audit fix --force`: it downgrades
Next.js.

## The pages

| Page | Who | What it shows |
| --- | --- | --- |
| `/` | everyone | What ORM_AI does, and whether the server, PostgreSQL and Redis are up |
| `/login` | everyone | Pick a demo user (on your own computer), or paste a token from a real login system |
| `/chat` | logged in | Your conversations on the left; the conversation in the middle; on the right, how the selected request went: each workflow stage, the sources it cited (with the passage), the tools it called and what it cost |
| `/approvals` | logged in | Everything waiting for a person in your shop, with the full approval slip; and every decision made |
| `/admin` | logged in | Requests by status, each agent's runs, errors and time (average and slowest 5%), each tool's calls and failures, recent requests, and the evaluation scores (admins) |
| `/workflows/{id}` | logged in | The full record of one request: answer, every agent step, every tool call with its arguments, approvals, stages and sources |

A shop's owner and staff see only their own shop. The admin (USR-101) sees every shop and
the evaluation scores, but a shop's own people make its decisions.

**The approval slip.** When the agents want to do something that changes money, stock,
prices or messages (a refund, a price change, a stock write-off, a supplier message), the
chat shows a slip instead of doing it: what, why, the estimate, the evidence and the rules.
Staff can decide staff-level actions; owner-level ones need the owner. **Approve** runs it,
**Reject** leaves everything as it was, and **Modify** lets you change the details first
(the record it applies to cannot be switched). The decision is stamped on the slip, and the
reply underneath lists only what a tool confirmed as done.

## Try the full approval example

This is the Phase 7 gate: the refund example, approval included, without a terminal.

1. Open <http://localhost:3000>, click **Log in**, and pick **Sunita Rao (staff)** of Sri
   Lakshmi Kirana Store (SHOP-001).
2. In the chat, click the suggestion **"The customer brought back SALE-005598 today. Please
   process the return."** (or type it).
3. Watch the right-hand panel: Understand the request, Look up records and rules,
   Investigate, then **Ask a person** turns red and waits.
4. Read the slip: a refund of about Rs 190, within the return window (POL-RETURNS-001).
   Click a rule chip to see the passage on the right.
5. Click **Approve**. The slip is stamped, the refund is carried out, and the reply says
   what was done. The badge on **Approvals** goes back to zero.
6. Click **Open the full record of this request** (bottom right) to see every agent step
   and tool call.

To see the owner rule: still as Sunita, ask **"Please change the selling price of PRD-0002
to Rs 125."** The slip says the owner decides. Log out, log in as **Ramesh Naidu (owner)**,
open **Approvals**, click **Review and decide**, then **Modify**, set Rs 124 and **Approve
with changes**.

Each demo action really changes the synthetic records: the refund is recorded once, and
approving the same refund again ends with "SALE-005598 is already returned". To start the demo afresh, reload the shops in the local Docker database
only: `python -m scripts.seed --reset` from `backend` (never against Supabase).

## How it talks to the backend

- **Logins** (`src/lib/login.ts`, `src/lib/api.ts`). The login page asks
  `GET /api/auth/dev-users` for the demo users and `POST /api/auth/dev-token` for a token.
  The token is kept in this browser's `localStorage` and sent as
  `Authorization: Bearer <token>` with every call. When the server answers 401 (expired,
  user switched off, or the backend restarted without an `AUTH_SECRET`), the website logs
  out and asks you to log in again. Set `AUTH_SECRET` in the backend's `.env` so logins
  survive a restart.
- **Live progress** (`src/lib/sse.ts`). The chat sends `POST /api/chat/stream` and reads
  the Server-Sent Events with `fetch` (the browser's `EventSource` cannot send a POST or a
  login header). Each `stage` event lights up a stage; `approval_requested` stops at
  **Ask a person**; `result` carries the same reply as `POST /api/chat`.
- **Decisions** go to `POST /api/approval/{workflow_id}`; the reply replaces the paused one.
- **Old conversations** come from `GET /api/sessions/{id}`, and each request's full reply
  (sources, actions, steps) from `GET /api/workflows/{id}` (its `reply` field, rebuilt from
  the saved graph state).
- **Lists** for the pages: `/api/sessions`, `/api/approvals`, `/api/workflows`,
  `/api/metrics`, `/api/evaluations`, `/api/shops` ([docs/api.md](../docs/api.md)).

Replies are Markdown, rendered with `react-markdown`, which never renders raw HTML. Citations
such as `[POL-RETURNS-001 v1 §3. How to refund]` become chips that open the passage.

## Layout of the code

```
src/
  app/                    the pages (each a thin wrapper around a component)
    page.tsx              home
    login/                LoginForm
    chat/                 the chat page
    approvals/            the approvals inbox
    admin/                the admin page
    workflows/[id]/       one request's full record
  components/
    chat/ChatWorkspace    conversations, entries, composer, live updates
    chat/RequestPanel     the stage rail, sources and tools (also used on the record page)
    ApprovalSlip          the slip: evidence, rules, Approve / Modify / Reject, the stamp
    ApprovalsInbox, AdminDashboard, WorkflowRecord, SiteHeader, BackendStatus, Markdown, ui
  lib/
    api.ts                every call to the backend, errors, the stream
    login.ts              who is logged in (localStorage + useSyncExternalStore)
    sse.ts                Server-Sent Events parser            (tested: sse.test.ts)
    turns.ts              the chat's model: entries and stages (tested: turns.test.ts)
    paths.ts              safe redirects after logging in      (tested: paths.test.ts)
    format.ts             plain-language labels, times, numbers
  types/api.ts            the API's shapes, mirroring backend/app/models/schemas.py
```

All pages are client components: they need the login, which lives in the browser. The
server only sends the static shell, so the site can be hosted anywhere (Vercel in Phase 9).

## The look

The design takes its cues from the shop ledger (bahi-khata): cool ruled paper, navy ink,
the red of the ledger's binding (and its double red margin line down the conversation), and
a green rubber stamp for what a person approved. The type is Mukta for text and Martel for
headings, both by Ek Type (an Indian foundry), self-hosted from `@fontsource` packages so a
build never calls Google Fonts; both also cover Devanagari for Hindi later. Colours are CSS
variables in `src/app/globals.css`, with a dark version. Only two things move: the stage
that is working, and the stamp when a decision lands (both off when the system asks for
reduced motion).

## Troubleshooting

| What you see | Why, and what to do |
| --- | --- |
| `npm` says `EBADENGINE` or `next` will not start | Node is older than 22.18. Install the LTS (above) and reopen Cursor. |
| The server card says **Not reachable** | The backend is not running, or `.env.local` points elsewhere. Start `uvicorn app.main:app --reload` in `backend`. |
| Login page: "This server does not offer demo logins" | The backend runs with `APP_ENV` other than `local`, or `AUTH_DEV_LOGIN=false`. Use a token instead. |
| In the browser console: "blocked by CORS policy" | The backend's `CORS_ORIGINS` must include the website's address (`http://localhost:3000` locally). Restart uvicorn after changing `.env`. |
| Logged out after restarting the backend | `AUTH_SECRET` is empty, so each start signs tokens with a new key. Set it in `.env`. |
| A request says the model is not available | The backend cannot reach the model. Run `python -m scripts.check_llm` in `backend`. |
| "Too many requests" | The agent limit (`RATE_LIMIT_AGENT`, 20 a minute) was reached; wait the seconds shown. |
| `NEXT_PUBLIC_API_URL` changed but nothing happened | `NEXT_PUBLIC_` values are read when `npm run dev` or `npm run build` starts. Restart it. |
