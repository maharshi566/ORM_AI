# docker/

Support files for `docker-compose.yml`.

- `postgres/init/`: SQL run once when the Postgres volume is first created (creates the `orm_ai_test` database).

The backend image is built from `backend/Dockerfile`, the website's from `frontend/Dockerfile`.
`docker-compose.prod.yml` (repository root) runs both in production on one server; see
[docs/deploy.md](../docs/deploy.md). To re-run the init scripts, remove the volume first:
`docker compose down -v` (this deletes all local database data).
