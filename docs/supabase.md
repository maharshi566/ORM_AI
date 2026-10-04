# Using Supabase as the database

Supabase is hosted PostgreSQL, so ORM_AI runs on it unchanged. Only the connection
string differs.

**Recommended setup:**

- **Local development and tests:** the Docker Postgres from `docker-compose.yml`.
  It is free and disposable, and `python -m scripts.seed --reset` can wipe it
  safely.
- **Shared demo or deployment:** Supabase.
- **Redis:** Supabase does not provide Redis. Keep Docker Redis locally and use a
  hosted Redis (for example Upstash) when you deploy.

## 1. Get the connection string

1. In your Supabase project, click **Connect**.
2. Choose the **Session pooler** connection string. It works over IPv4 on every
   plan, and it supports the prepared statements that our database driver uses.
   (Most home networks in India have no IPv6, so the "Direct connection" may not
   reach Supabase unless you buy its IPv4 add-on.)
3. It looks like this:

   ```
   postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@<pooler-host>.pooler.supabase.com:5432/postgres
   ```

## 2. Put it in `.env`

Change the start to `postgresql+asyncpg://` and add `?ssl=require` at the end:

```
DATABASE_URL=postgresql+asyncpg://postgres.<project-ref>:<password>@<pooler-host>.pooler.supabase.com:5432/postgres?ssl=require
```

If your password contains special characters such as `@ : / ? # %`, URL-encode
them (for example `@` becomes `%40`), or choose a password without them.

**Transaction pooler (port 6543)?** Only for serverless hosting. If you use it,
also set `DB_TRANSACTION_POOLER=true`. That switches off prepared statements,
which the transaction pooler does not support.

## 3. Create the tables and load the data

From `backend/`:

```powershell
alembic upgrade head
python -m scripts.seed
```

Open **Table Editor** in Supabase: you should see the 24 tables, with data in the
shop tables.

**Be careful:** `python -m scripts.seed --reset` wipes **every ORM_AI table** in
whatever database `DATABASE_URL` points to. Never point `TEST_DATABASE_URL` at
Supabase either: the migration test drops all tables.

## 4. Why Row Level Security is on

Supabase publishes every table in the `public` schema through its Data API. Its
guidance is to enable Row Level Security (RLS) on every table in an exposed
schema; a table without it can be read and written by any role that has a grant
on it, including the public key.
The second migration enables RLS on all ORM_AI tables and adds no policies, so:

- the Data API (publishable/anon key) can read **nothing**,
- the ORM_AI backend is unaffected, because it connects as the table owner, which
  bypasses RLS.

The Supabase dashboard will therefore show the tables as "RLS enabled". That is
intended.

## 5. Later options

- **Supabase Auth** could provide logins for shop owners and staff in Phase 6;
  the backend would verify its JWTs.
- **pgvector** in Supabase could hold embeddings, but the project spec uses
  ChromaDB, so Phase 3 stays with ChromaDB.

Source: [Supabase: Connect to your database](https://supabase.com/docs/guides/database/connecting-to-postgres),
[Supabase: Row Level Security](https://supabase.com/docs/guides/database/postgres/row-level-security).
