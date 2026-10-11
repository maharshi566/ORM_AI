# syntax=docker/dockerfile:1

# ---- deps: build wheels and install into an isolated prefix ----
FROM python:3.12-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ---- runtime: small image, non-root user ----
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOG_JSON=true
WORKDIR /app

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data/chroma /app/data/uploads \
    && chown -R appuser:appuser /app/data

COPY --from=deps /install /usr/local
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser migrations ./migrations
COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser knowledge_base ./knowledge_base
# The 45 evaluation cases, so the gate can run against a deployment's own model
# (docker compose exec backend python -m scripts.eval_agent); reports are written here.
COPY --chown=appuser:appuser evaluation ./evaluation

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "scripts.healthcheck"]

# Apply database migrations, then start the API on PORT (8000 unless the host sets it).
# SEED_IF_EMPTY and INGEST_ON_START add the demo data and the knowledge index
# (scripts/start.py).
CMD ["python", "-m", "scripts.start"]
