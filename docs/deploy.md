# Deploying ORM_AI (Phase 9)

Two ways to put ORM_AI on the internet. Both run the same images and the same production
rules.

- [Before you start](#before-you-start)
- [A. One server with Docker](#a-one-server-with-docker) (a small VPS; everything in one place)
- [B. Managed services](#b-managed-services) (backend on Render or Railway, website on Vercel)
- [A public demo](#a-public-demo)
- [After deploying](#after-deploying): first data, updates, backups, the evaluation
- [Security checklist](#security-checklist)
- [When something is wrong](#when-something-is-wrong)

## Before you start

You need:

| | Why |
| --- | --- |
| A chat model and its key | The agents. The same `.env` lines as on your computer ([agents.md](agents.md#7-choosing-the-model-openai-omniroute-or-openrouter)) |
| A secret for login tokens | `AUTH_SECRET`, at least 32 random characters: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| Two addresses | One for the website, one for the API (for example `shop.example.com` and `api.shop.example.com`) |

With `APP_ENV=production` the backend **refuses to start** when `AUTH_SECRET` is weak,
`AUTH_REQUIRED` is not `true`, or `CORS_ORIGINS` is `*`; the log says what to change. Both
setups below set `APP_ENV=production` for you.

## A. One server with Docker

`docker-compose.prod.yml` runs PostgreSQL, Redis, the API and the website. The database and
Redis are not reachable from outside; the API and the website listen on `127.0.0.1` only,
for an HTTPS proxy in front.

**1. Settings.** On the server, in the project folder:

```bash
cp .env.prod.example .env.prod
nano .env.prod      # fill in every value marked REQUIRED
```

`PUBLIC_API_URL` is the API's public address (the website's JavaScript calls it);
`CORS_ORIGINS` is the website's address. Compose stops with a clear message if a required
value is missing.

**2. Start.**

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build --wait
```

Migrations run when the backend starts. `--wait` returns once every service reports
healthy.

**3. Load the demo shops and the knowledge base** (once):

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec backend python -m scripts.seed
docker compose -f docker-compose.prod.yml --env-file .env.prod exec backend python -m scripts.ingest
```

**4. HTTPS.** Put a reverse proxy in front. [Caddy](https://caddyserver.com) gets
certificates by itself; this `Caddyfile` is all it needs:

```
shop.example.com {
    reverse_proxy 127.0.0.1:3000
}

api.shop.example.com {
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1    # pass the chat's live progress through at once
    }
}
```

`flush_interval -1` matters: without it the proxy may hold back the Server-Sent Events of
`POST /api/chat/stream`, and the stage tracker only fills in at the end.

## B. Managed services

**Database.** Any PostgreSQL 15+: Supabase ([supabase.md](supabase.md)), Neon, Render or
Railway. Paste the connection string as the provider gives it, `postgres://...?sslmode=require`
included: the backend turns it into the form its driver needs. For Supabase's transaction
pooler (port 6543) also set `DB_TRANSACTION_POOLER=true`.

**Redis.** Upstash, Render Key Value or Railway Redis. Set `REDIS_URL` to it, and
`RATE_LIMIT_STORAGE` to the same URL (another database number, for example `/1`) so limits
are shared by every backend process.

**Backend: Render or Railway.** Create a web service from this repository, built from
`backend/Dockerfile` (root directory `backend`). Health check path: `/api/health`. Set:

| Variable | Value |
| --- | --- |
| `APP_ENV` | `production` |
| `AUTH_REQUIRED` | `true` |
| `AUTH_SECRET` | your secret (Render can generate one) |
| `CORS_ORIGINS` | the website's address, e.g. `https://orm-ai.vercel.app` |
| `DATABASE_URL`, `REDIS_URL`, `RATE_LIMIT_STORAGE` | from the steps above |
| `LLM_*`, `EMBEDDING_*`, `OPENAI_API_KEY` | as in your local `.env` |
| `BUSINESS_DATE` | `2026-09-30` for the demo shops |
| `UPLOAD_DIR`, `CHROMA_PERSIST_DIR` | `/app/data/uploads`, `/app/data/chroma` on a persistent disk |

Uploads and the knowledge index are files. Give the service a **persistent disk** mounted
at `/app/data`; without one they are lost on every deploy (then re-run the ingestion after
each deploy). Then open the service's shell once and run `python -m scripts.seed` and
`python -m scripts.ingest`.

**Website: Vercel.** Import the repository, set the root directory to `frontend`, and add
the environment variable `NEXT_PUBLIC_API_URL` with the backend's public address. Vercel
detects Next.js and builds it. `NEXT_PUBLIC_API_URL` is built into the JavaScript: after
changing it, redeploy. Finally put the Vercel address in the backend's `CORS_ORIGINS`.

## A public demo

On a shared server, development logins are off: every request needs a token from your own
login system (signed with `AUTH_SECRET`). For a public demo on the synthetic shops, list
the accounts visitors may use:

```
AUTH_DEMO_USERS=USR-001,USR-002,USR-003,USR-004
```

The login page then offers exactly those accounts. An **admin is never allowed** this way,
even if listed: an admin token reads and decides for every shop. Visitors can approve
actions, which changes the demo records; reset them now and then:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec backend python -m scripts.seed --reset
docker compose -f docker-compose.prod.yml --env-file .env.prod exec backend python -m scripts.ingest
```

(`--reset` wipes every table, the knowledge base's bookkeeping included, so the ingestion runs
again.) Leave `AUTH_DEMO_USERS` empty on a deployment with real shop data.

## After deploying

- **Check it:** `https://api.your-domain/api/health` says `"status": "ok"`; the website's
  home page shows **All systems up**.
- **Update:** `git pull`, then the same `up -d --build --wait` command. Migrations run on start.
- **Back up the database:**
  `docker compose -f docker-compose.prod.yml --env-file .env.prod exec postgres pg_dump -U orm_ai orm_ai > backup.sql`
- **Run the evaluation against the deployed model:** the 40 cases ship with the image.
  `docker compose -f docker-compose.prod.yml --env-file .env.prod exec backend python -m scripts.eval_agent --temperature 0 --pause 30`.
  The admin page (as an admin) shows the run.
- **Logs:** `docker compose -f docker-compose.prod.yml logs -f backend` (JSON lines; phone
  numbers and email addresses are masked).

## Security checklist

- [ ] `AUTH_SECRET` is long and random, and only in the server's settings. Changing it logs
      everyone out.
- [ ] `CORS_ORIGINS` is exactly the website's address.
- [ ] Both addresses are HTTPS. Tokens travel in a header; without HTTPS anyone on the path
      can read them.
- [ ] The model and embedding keys are set on the backend only. The website gets
      `NEXT_PUBLIC_API_URL` and nothing else.
- [ ] `AUTH_DEMO_USERS` is empty unless this is the public demo.
- [ ] PostgreSQL and Redis are not reachable from the internet (the production Compose file
      does not publish them).
- [ ] Backups run, and a restore has been tried once.

## When something is wrong

| What you see | Cause and fix |
| --- | --- |
| The backend restarts with "AUTH_SECRET must be at least 32 random characters" (or AUTH_REQUIRED, CORS_ORIGINS) | A production setting is unsafe; the log line says which |
| `required variable ... is missing a value` from Compose | A REQUIRED value is empty in `.env.prod` |
| The website loads but says the server cannot be reached | `PUBLIC_API_URL` / `NEXT_PUBLIC_API_URL` is wrong, or the API's address is not HTTPS while the site is; rebuild the website after fixing it |
| The browser console shows a CORS error | `CORS_ORIGINS` does not match the website's address exactly (scheme, host, no trailing slash) |
| The chat's stages appear only at the end | The proxy buffers the stream: `flush_interval -1` in Caddy, `proxy_buffering off` in nginx |
| "This server does not offer demo logins" | Expected on a real deployment; for a demo set `AUTH_DEMO_USERS` |
| Answers cite nothing | The knowledge base was not ingested: run `python -m scripts.ingest` in the backend container |
