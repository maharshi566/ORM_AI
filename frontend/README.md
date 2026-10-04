# ORM_AI frontend

Next.js 16 (App Router) + TypeScript + Tailwind CSS. It talks only to the FastAPI backend at `NEXT_PUBLIC_API_URL`.

```powershell
Copy-Item .env.example .env.local
npm install
npm run dev        # http://localhost:3000
```

| Command | What it does |
| --- | --- |
| `npm run dev` | Development server with hot reload |
| `npm run lint` | ESLint |
| `npm run typecheck` | TypeScript type check |
| `npm run build` | Production build (what Vercel runs) |

## Layout

```
src/
  app/            pages: / (home + backend status), /chat, /approvals, /admin
  components/     SiteHeader, BackendStatus, ComingSoon
  lib/api.ts      fetch helpers for the backend
  types/api.ts    response shapes, mirroring backend/app/models/schemas.py
```

`npm install` reports a few "high severity" advisories in ESLint's own dependencies. They affect lint tooling only, not the app. Don't run `npm audit fix --force`: it downgrades Next.js to version 14.
