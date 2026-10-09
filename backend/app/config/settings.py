"""Application settings, loaded from environment variables and .env files.

Every setting can be overridden with an environment variable of the same name in
upper case (for example ``DATABASE_URL``). Values are read from, in order of
priority: real environment variables, ``backend/.env``, then the repo-root ``.env``.
"""

from datetime import date
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Later files win: backend/.env overrides the repo-root .env.
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # "OPENAI_API_KEY=" or "BUSINESS_DATE=" with nothing after it means "not set",
        # so the default applies instead of failing to parse an empty value.
        env_ignore_empty=True,
    )

    # --- App -------------------------------------------------------------
    app_name: str = "ORM_AI"
    app_version: str = "0.1.0"
    app_env: Literal["local", "test", "staging", "production"] = "local"
    log_level: str = "INFO"
    log_json: bool = False

    # --- API -------------------------------------------------------------
    api_prefix: str = "/api"
    # Comma-separated list, e.g. "http://localhost:3000,https://orm-ai.vercel.app"
    cors_origins: str = "http://localhost:3000"

    # --- Data stores -----------------------------------------------------
    database_url: str = "postgresql+asyncpg://orm_ai:orm_ai@localhost:5432/orm_ai"
    # Set true only when DATABASE_URL points at a transaction-mode pooler such as
    # Supabase's port 6543. It turns off prepared statements, which those poolers break.
    db_transaction_pooler: bool = False
    redis_url: str = "redis://localhost:6379/0"

    # --- Knowledge base and retrieval (Phase 3) ---------------------------
    # Relative paths start at backend/ (see app/config/paths.py).
    knowledge_base_dir: str = "./knowledge_base"
    chroma_persist_dir: str = "./data/chroma"
    # heuristic (default, free), llm (scores results with LLM_MODEL_FAST), or none
    reranker: Literal["heuristic", "llm", "none"] = "heuristic"
    # Results less similar than this (and without the question's keywords) count as
    # "not found". Empty = the embedding model's default; the retrieval evaluation
    # prints the range to choose from.
    retrieval_min_similarity: float | None = None

    # --- LLM (embeddings from Phase 3, agents from Phase 4) ----------------
    openai_api_key: SecretStr | None = None
    llm_model_fast: str = ""  # triage, validation, LLM-as-judge
    llm_model_smart: str = ""  # investigation, final response
    embedding_model: str = "text-embedding-3-small"
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 3
    # Tried once when the main model still fails after its retries (not for a bad key).
    llm_model_fallback: str = ""
    # auto: strict JSON schema, falling back to JSON mode for models that refuse it.
    llm_structured_output: Literal["auto", "json_schema", "json_object"] = "auto"
    # For reasoning models only (e.g. low): sent as reasoning_effort. Empty = not sent.
    llm_reasoning_effort: str = ""

    # --- Optional gateway (OmniRoute, LiteLLM, Ollama, ...) ----------------
    # Any server that speaks the OpenAI API. Empty LLM_BASE_URL = OpenAI itself.
    # See docs/omniroute.md and app/services/llm_service.py for how these combine.
    llm_base_url: str = ""  # e.g. http://localhost:20128/v1
    llm_api_key: SecretStr | None = None  # the gateway's key; empty = use OPENAI_API_KEY
    # Embeddings may go elsewhere than chat. Empty = same place as chat.
    embedding_base_url: str = ""
    embedding_api_key: SecretStr | None = None

    # --- Agents (Phase 4) -------------------------------------------------
    # database: workflow state is saved in PostgreSQL after every step (needed to pause
    # for approval and resume later). memory: kept in the process only (tests, demos).
    checkpointer: Literal["database", "memory"] = "database"
    agent_max_loops: int = 2  # "need more data" and "rewrite the answer" loops, each
    agent_max_tool_calls: int = 8  # per data-retrieval visit
    session_memory_turns: int = 10  # earlier messages the agents see
    session_ttl_hours: int = 24  # how long Redis keeps a quiet conversation

    # --- Tracing (used from Phase 4) -------------------------------------
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "orm-ai"

    # --- Business date ---------------------------------------------------
    # Freezes "today" for the synthetic data (generated up to 2026-09-30), so that
    # "days overdue" and "late by" match the planted edge cases. Leave empty in production.
    business_date: date | None = None

    # --- Mock external APIs (Phase 2) -------------------------------------
    # none, timeout, server_error, not_found, rate_limited, or random (fails 20% of calls)
    mock_api_failure_mode: str = "none"
    mock_api_latency_ms: int = 0
    tool_retry_backoff_seconds: float = 0.3

    # --- Health checks ---------------------------------------------------
    health_check_timeout_seconds: float = 2.0

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings (cached after the first call)."""
    return Settings()
