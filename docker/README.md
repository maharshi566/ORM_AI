# docker/

Support files for `docker-compose.yml`.

- `postgres/init/`: SQL run once when the Postgres volume is first created (creates the `orm_ai_test` database).

The backend image is built from `backend/Dockerfile`. To re-run the init scripts, remove the volume first:
`docker compose down -v` (this deletes all local database data).
