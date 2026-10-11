"""Application settings, loaded from environment variables and .env files.

Every setting can be overridden with an environment variable of the same name in
upper case (for example ``DATABASE_URL``). Values are read from, in order of
priority: real environment variables, ``backend/.env``, then the repo-root ``.env``.
"""

import re
from datetime import date
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
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
    # Hosting providers' URLs work as given (postgres://... ?sslmode=require): see
    # _async_driver below.
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
    # Sampling temperature. Unset = the provider's default. 0 makes evaluation runs
    # repeatable (scripts/eval_agent --temperature 0); reasoning models reject it.
    llm_temperature: float | None = Field(default=None, ge=0, le=2)

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
    # Phase 5: after its checks in code pass, the validator also asks the fast model
    # whether every claim is supported (one more call per reply). Off by default.
    validator_llm_judge: bool = False
    session_memory_turns: int = 10  # earlier messages the agents see
    session_ttl_hours: int = 24  # how long Redis keeps a quiet conversation

    # --- API layer (Phase 6) ----------------------------------------------
    # Signed login tokens. Empty secret: a random one per process (tokens stop working
    # after a restart). Set AUTH_SECRET in production, and AUTH_REQUIRED=true.
    auth_secret: SecretStr | None = None
    auth_required: bool = False  # true: every chat/approval/record call needs a token
    auth_token_hours: int = 12
    # POST /api/auth/dev-token gives a token for any user ID. Never on in production.
    auth_dev_login: bool = True
    # A public demo: these user IDs (comma-separated, never an admin) may log in with
    # the demo login on any server, production included. Empty = no demo logins there.
    auth_demo_users: str = ""
    # Real logins (app/services/auth_service.py): email + password, and a one-time code
    # sent to the user's phone. otp_channel: whatsapp (the WhatsApp settings below),
    # console (the code is written to the backend log: local and test only), or off.
    # Empty: console on a developer's machine, off anywhere else (see otp_mode).
    auth_password_login: bool = True
    otp_channel: Literal["whatsapp", "console", "off"] | None = None
    rate_limit_enabled: bool = True
    rate_limit_agent: str = "20/minute"  # chat, agent run, approvals (per user or IP)
    rate_limit_upload: str = "10/hour"
    # memory (one process) or a Redis URL such as redis://localhost:6379/1
    rate_limit_storage: str = "memory"
    upload_dir: str = "./data/uploads"
    upload_max_mb: float = 5.0

    # --- Tracing (used from Phase 4) -------------------------------------
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "orm-ai"

    # --- Business date ---------------------------------------------------
    # Freezes "today" for the synthetic data (generated up to 2026-09-30), so that
    # "days overdue" and "late by" match the planted edge cases. Leave empty in production.
    business_date: date | None = None

    # --- Messaging: WhatsApp Cloud API (docs/whatsapp.md) --------------------
    # mock: nothing leaves the backend (development, tests). whatsapp: payment reminders,
    # supplier follow-ups, login codes and the morning briefing go out through Meta's API.
    messaging_provider: Literal["mock", "whatsapp"] = "mock"
    whatsapp_access_token: SecretStr | None = None  # a System User token from Meta
    whatsapp_phone_number_id: str = ""  # the sending number's ID (not the number itself)
    whatsapp_api_version: str = "v23.0"
    whatsapp_app_secret: SecretStr | None = None  # checks that webhook calls come from Meta
    whatsapp_verify_token: SecretStr | None = None  # your own string, for the webhook setup
    # When set, every message goes to this number instead of the real recipient (testing).
    whatsapp_test_recipient: str = ""
    whatsapp_language: str = "en"  # the templates' language code in WhatsApp Manager
    whatsapp_template_payment_reminder: str = "payment_reminder"
    whatsapp_template_supplier_follow_up: str = "supplier_follow_up"
    whatsapp_template_login_code: str = "login_code"
    whatsapp_template_daily_briefing: str = "daily_briefing"

    # --- Mock external APIs (Phase 2) -------------------------------------
    # none, timeout, server_error, not_found, rate_limited, or random (fails 20% of calls)
    mock_api_failure_mode: str = "none"
    mock_api_latency_ms: int = 0
    tool_retry_backoff_seconds: float = 0.3

    # --- Health checks ---------------------------------------------------
    health_check_timeout_seconds: float = 2.0

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, value: str) -> str:
        """Render, Railway, Neon and Heroku hand out ``postgres://`` or ``postgresql://``
        URLs with ``?sslmode=require``. The app talks to PostgreSQL through asyncpg, which
        needs ``postgresql+asyncpg://`` and spells the SSL option ``ssl=``."""
        url = value.strip()
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                url = "postgresql+asyncpg://" + url.removeprefix(prefix)
                break
        if url.startswith("postgresql+asyncpg://"):
            url = re.sub(r"([?&])sslmode=", lambda match: f"{match.group(1)}ssl=", url)
        return url

    @property
    def demo_user_list(self) -> list[str]:
        return [user.strip().upper() for user in self.auth_demo_users.split(",") if user.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def otp_mode(self) -> Literal["whatsapp", "console", "off"]:
        """How one-time login codes are delivered (OTP_CHANNEL, or the default)."""
        if self.otp_channel is not None:
            return self.otp_channel
        return "console" if self.app_env in {"local", "test"} else "off"

    def production_problems(self) -> list[str]:
        """What must change before the app may start with APP_ENV=production."""
        problems = []
        secret = self.auth_secret.get_secret_value().strip() if self.auth_secret else ""
        if len(secret) < 32:
            problems.append(
                "AUTH_SECRET must be at least 32 random characters (for example the output "
                'of: python -c "import secrets; print(secrets.token_urlsafe(48))").'
            )
        if not self.auth_required:
            problems.append("AUTH_REQUIRED must be true.")
        if "*" in self.cors_origin_list:
            problems.append("CORS_ORIGINS must list the frontend's address, not *.")
        if self.otp_mode == "console":
            problems.append(
                "OTP_CHANNEL=console writes login codes to the log; use whatsapp or off."
            )
        if self.otp_mode == "whatsapp" or self.messaging_provider == "whatsapp":
            problems.extend(self.whatsapp_problems())
        return problems

    def whatsapp_problems(self) -> list[str]:
        """What is missing before messages can go out through WhatsApp."""
        problems = []
        token = (
            self.whatsapp_access_token.get_secret_value().strip()
            if self.whatsapp_access_token
            else ""
        )
        if not token:
            problems.append("WHATSAPP_ACCESS_TOKEN is empty (a System User token from Meta).")
        if not self.whatsapp_phone_number_id.strip():
            problems.append("WHATSAPP_PHONE_NUMBER_ID is empty (WhatsApp Manager > API Setup).")
        return problems


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings (cached after the first call)."""
    return Settings()
