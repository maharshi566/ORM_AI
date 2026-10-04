from pathlib import Path

from app.config.settings import Settings
from app.core.logging import MASK, mask_sensitive_fields
from app.graph.state import AgentState
from app.prompts.triage_prompt import TRIAGE_PROMPT


def test_tests_do_not_see_the_developers_own_settings() -> None:
    # conftest hides real environment variables and .env files from every test. Without
    # that, a developer with OPENAI_API_KEY set (in a shell or in backend/.env) got
    # different test results from everyone else.
    settings = Settings()

    assert settings.openai_api_key is None
    assert settings.embedding_model == "text-embedding-3-small"
    assert settings.database_url.endswith("@localhost:5432/orm_ai")


def test_empty_values_in_env_file_mean_not_set(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "RETRIEVAL_MIN_SIMILARITY=\nBUSINESS_DATE=\nOPENAI_API_KEY=\nRERANKER=llm\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env)

    assert settings.retrieval_min_similarity is None
    assert settings.business_date is None
    assert settings.openai_api_key is None
    assert settings.reranker == "llm"


def test_cors_origins_are_split_and_trimmed() -> None:
    settings = Settings(_env_file=None, cors_origins=" http://a.test , https://b.test ,")

    assert settings.cors_origin_list == ["http://a.test", "https://b.test"]


def test_secrets_are_not_printed_in_settings_repr() -> None:
    settings = Settings(_env_file=None, openai_api_key="sk-should-not-appear")

    assert "sk-should-not-appear" not in repr(settings)


def test_sensitive_log_fields_are_masked() -> None:
    event = {"event": "login", "api_key": "sk-123", "Authorization": "Bearer x", "shop": "A1"}

    masked = mask_sensitive_fields(None, "info", dict(event))

    assert masked["api_key"] == MASK
    assert masked["Authorization"] == MASK
    assert masked["shop"] == "A1"


def test_skeleton_modules_import() -> None:
    state: AgentState = {"workflow_id": "wf-1", "user_query": "How much rice is left?"}

    assert state["workflow_id"] == "wf-1"
    assert TRIAGE_PROMPT.render().startswith("## Role")
