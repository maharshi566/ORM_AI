from app.config.settings import Settings
from app.core.logging import MASK, mask_sensitive_fields
from app.graph.state import AgentState
from app.prompts.triage_prompt import TRIAGE_PROMPT


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
