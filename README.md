# ORM_AI

A multi-agent AI assistant that helps local shopkeepers keep their records organised and detailed.

> **Status:** Phase 0 (foundations) is done. The API, config, logging, Docker setup, tests and CI work. The agents, RAG pipeline and UI pages are placeholders, filled in phase by phase. See [Roadmap](#roadmap).

## Problem statement

Local shops in the area run on memory, notebooks and scattered WhatsApp messages. Stock counts drift, supplier orders are placed late, customer credit (*udhaar*) is hard to track, and nothing is easy to look up later.

ORM_AI gives shopkeepers one assistant that:

- answers questions about stock, sales, suppliers and customer credit from the shop's own records
- checks the shop's rules (credit limits, supplier terms, pricing) and cites them
- drafts actions such as reorders, payment reminders and price updates
- asks the shopkeeper before anything that moves money or stock

**Primary users:** owners and staff of local shops.
**Working scope:** the exact record types are settled in Phase 1.

## Architecture (target)

```mermaid
flowchart TD
    U[Shopkeeper] --> FE[Next.js frontend]
    FE -->|REST + SSE| API[FastAPI backend]
    API --> SUP[Supervisor agent]
    SUP --> TRI[Triage] & DATA[Data retrieval] & KNOW[Knowledge / RAG] & INV[Investigation]
    INV --> GATE{Needs approval?}
    GATE -->|yes| HUMAN[Human approval]
    GATE -->|no| ACT[Action agent]
    HUMAN --> ACT
    ACT --> RESP[Response agent] --> VAL{Validator}
    VAL -->|PASS| FE
    DATA --> PG[(PostgreSQL)]
    KNOW --> CH[(ChromaDB)]
    API --> RD[(Redis)]
```

## Tech stack

| Layer | Tools |
| --- | --- |
| Frontend | Next.js 16 (App Router), TypeScript, Tailwind CSS 4 |
| Backend | Python 3.12, FastAPI, Pydantic v2, structlog |
| Agents | LangGraph + LangChain (Phase 4), OpenAI API |
| Data | PostgreSQL 17, Redis 7, ChromaDB (Phase 3) |
| Quality | Pytest, Ruff, ESLint, GitHub Actions, pre-commit |
| Deploy | Docker, Vercel (frontend), Render or Railway (backend) |

## Folder structure

```
ORM_AI/
├── backend/
│   ├── app/
│   │   ├── main.py            FastAPI app: middleware, error handlers, routers
│   │   ├── api/               routers; routes/health.py is live
│   │   ├── config/            settings loaded from .env
│   │   ├── core/              logging, request-ID middleware, error handling
│   │   ├── models/            Pydantic schemas, SQLAlchemy database
│   │   ├── services/          health checks, Redis, LLM and memory services
│   │   ├── agents/            8 agents (placeholders until Phases 4–5)
│   │   ├── graph/             LangGraph state, nodes, edges, workflow
│   │   ├── rag/               loaders, chunking, embeddings, vector store (Phase 3)
│   │   ├── tools/             typed read and action tools (Phase 2)
│   │   └── prompts/           one prompt per agent, fixed structure
│   ├── tests/                 pytest suite
│   ├── scripts/               seed.py (Phase 1), ingest.py (Phase 3)
│   ├── data/                  synthetic data, local vector store
│   ├── knowledge_base/        shop policies and documents for RAG
│   ├── evaluation/            evaluation datasets and reports (Phase 8)
│   ├── Dockerfile
│   ├── requirements.txt / requirements-dev.txt
│   └── pyproject.toml         Ruff and pytest settings
├── frontend/
│   └── src/
│       ├── app/               pages: /, /chat, /approvals, /admin
│       ├── components/        SiteHeader, BackendStatus, ComingSoon
│       ├── lib/api.ts         backend client (uses NEXT_PUBLIC_API_URL)
│       └── types/api.ts       response types
├── docker/postgres/init/      creates the test database on first start
├── .github/workflows/ci.yml   lint, tests and build on every push
├── docker-compose.yml         Postgres, Redis and the backend
└── .env.example               every setting, documented
```

## Prerequisites (Windows)

- [Python 3.12](https://www.python.org/downloads/) (tick "Add python.exe to PATH")
- [Node.js 22 LTS](https://nodejs.org/)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [Git](https://git-scm.com/download/win)

## Run it locally

All commands are for PowerShell, from the project root.

**1. Create your `.env`**

```powershell
Copy-Item .env.example .env
```

Change `POSTGRES_PASSWORD`, and set the same password inside `DATABASE_URL`.

**2. Start PostgreSQL and Redis**

```powershell
docker compose up -d postgres redis
docker compose ps        # both should say "healthy"
```

**3. Start the backend**

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

- Health check: <http://localhost:8000/api/health> should return `"status": "ok"`.
- API docs: <http://localhost:8000/docs>

If PowerShell blocks `Activate.ps1`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

**4. Start the frontend** (in a second terminal)

```powershell
cd frontend
Copy-Item .env.example .env.local
npm install
npm run dev
```

Open <http://localhost:3000>. The Backend status card should say **All systems up**.

**Or run the whole backend stack in Docker**

```powershell
docker compose up --build
```

## Tests and checks

```powershell
cd backend
pytest                 # 13 tests
ruff check .
ruff format --check .

cd ..\frontend
npm run lint
npm run typecheck
npm run build
```

GitHub Actions runs all of these on every push and pull request (`.github/workflows/ci.yml`).

## Put it on GitHub

```powershell
git init -b main
pip install pre-commit; pre-commit install
git add .
git commit -m "Phase 0: project skeleton"
git remote add origin https://github.com/<your-username>/ORM_AI.git
git push -u origin main
```

## API (so far)

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/health` | 200 when PostgreSQL and Redis answer, 503 with details when one is down |

Every response carries an `X-Request-ID` header, and every error uses one shape:

```json
{ "error": { "code": "not_found", "message": "Not Found", "request_id": "…", "details": null } }
```

## Roadmap

| Phase | Weeks | What gets built |
| --- | --- | --- |
| **0 Foundations** | 1 | Repo, config, logging, health check, Docker, CI ✅ |
| 1 Data + docs | 2 | Database tables, Alembic, synthetic shop data, knowledge-base documents |
| 2 Tools | 3 | Typed read and action tools with mock APIs and failure injection |
| 3 RAG | 4–5 | Chunking, embeddings, ChromaDB, retriever, reranker, citations |
| 4 Agent graph | 6–7 | LangGraph state graph, 8 agents, Postgres checkpointer, `/api/chat` |
| 5 Approval + guardrails | 8 | Policy gate, `interrupt()` approval, validator, input guardrails |
| 6 API | 9 | All endpoints, SSE progress stream, rate limits, uploads |
| 7 Frontend | 10 | Chat, workflow, sources, approval and admin pages |
| 8 Evaluation | 11 | 40-case evaluation set, metrics, tracing, report |
| 9 Deploy | 12 | Vercel + Render/Railway, full README, demo |

## Security notes

- Secrets live only in `.env` (git-ignored). The frontend gets `NEXT_PUBLIC_API_URL` and nothing else.
- Logs mask keys that look like passwords, tokens or API keys.
- Health errors show only the exception type, never hosts or credentials.
- `npm install` reports advisories in ESLint's dependencies. They affect lint tooling, not the app. Don't run `npm audit fix --force`: it downgrades Next.js.
