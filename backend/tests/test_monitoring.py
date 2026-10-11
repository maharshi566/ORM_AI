"""Monitoring: the metrics endpoint and what it counts, Sentry's scrubbing, the liveness
check and errors reported by the website."""

from app.core.exceptions import NotFoundError
from app.core.monitoring import REGISTRY, _scrub
from tests.test_api_phase6 import LATE_PO, make_client, token_for  # noqa: F401  (fixture)


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_metrics_count_requests_by_route_template(make_client) -> None:  # noqa: F811
    api = make_client(rate_limit_enabled=False)
    route = "/workflows/{workflow_id}"  # the template within the API, without /api
    before = sample("orm_http_requests_total", method="GET", route=route, status="404")

    await api.get("/api/workflows/not-a-real-id")
    text = (await api.get("/metrics")).text

    assert sample("orm_http_requests_total", method="GET", route=route, status="404") == before + 1
    assert "orm_http_request_seconds_bucket" in text and "not-a-real-id" not in text


async def test_a_chat_counts_its_workflow_steps_tokens_and_tools(make_client) -> None:  # noqa: F811
    api = make_client(rate_limit_enabled=False)
    workflows = sample("orm_workflows_total", outcome="completed")
    triage = sample("orm_agent_steps_total", agent="triage", status="success")
    tokens = sample("orm_llm_tokens_total", model="fake-model", direction="input")

    response = await api.post(
        "/api/chat",
        json={"shop_id": "SHOP-001", "message": "What were my total sales last week?"},
        headers=await token_for(api, "USR-001"),
    )

    assert response.status_code == 200, response.text
    assert sample("orm_workflows_total", outcome="completed") == workflows + 1
    assert sample("orm_agent_steps_total", agent="triage", status="success") == triage + 1
    assert sample("orm_llm_tokens_total", model="fake-model", direction="input") > tokens
    assert sample("orm_tool_calls_total", tool="get_sales_summary", status="success") >= 1


async def test_logins_are_counted_by_result(make_client) -> None:  # noqa: F811
    api = make_client(rate_limit_enabled=False)
    failed = sample("orm_logins_total", method="password", result="failed")

    await api.post("/api/auth/login", json={"email": "nobody@example.com", "password": "x"})

    assert sample("orm_logins_total", method="password", result="failed") == failed + 1


async def test_metrics_need_the_token_when_one_is_set(make_client) -> None:  # noqa: F811
    api = make_client(metrics_token="a-long-scrape-token")

    nothing = await api.get("/metrics")
    wrong = await api.get("/metrics", headers={"Authorization": "Bearer guess"})
    right = await api.get("/metrics", headers={"Authorization": "Bearer a-long-scrape-token"})

    assert nothing.status_code == wrong.status_code == 401
    assert right.status_code == 200 and "orm_build_info" in right.text


async def test_metrics_are_off_in_production_without_a_token(make_client) -> None:  # noqa: F811
    api = make_client(app_env="production", auth_required=True)

    assert (await api.get("/metrics")).status_code == 404


async def test_the_liveness_check_needs_no_database(make_client) -> None:  # noqa: F811
    api = make_client()

    response = await api.get("/api/health/live")

    assert response.status_code == 200 and response.json() == {"status": "ok"}


async def test_the_website_can_report_an_error(make_client, caplog) -> None:  # noqa: F811
    api = make_client(rate_limit_enabled=False)
    before = sample("orm_client_errors_total")

    response = await api.post(
        "/api/client-errors",
        json={
            "kind": "render",
            "message": "Cannot read 'name' for +91 98765 43210",
            "page": "/chat?session=abc",
        },
    )

    assert response.status_code == 202
    assert sample("orm_client_errors_total") == before + 1
    logged = caplog.text
    assert "98765 43210" not in logged and "session=abc" not in logged


# ------------------------------------------------------------------ Sentry scrubbing


def test_sentry_gets_no_bodies_cookies_secrets_or_phone_numbers() -> None:
    event = {
        "request": {
            "data": {"message": "Remind CUST-0004"},
            "cookies": {"session": "x"},
            "headers": {"Authorization": "Bearer t", "X-Cron-Secret": "s", "User-Agent": "UA"},
            "query_string": "phone=+919876543210",
        },
        "exception": {"values": [{"value": "Could not message +91 98765 43210"}]},
    }

    scrubbed = _scrub(event, {})

    request = scrubbed["request"]
    assert "data" not in request and "cookies" not in request
    assert request["headers"] == {"User-Agent": "UA"}
    assert "9876543210" not in request["query_string"]
    assert "98765" not in scrubbed["exception"]["values"][0]["value"]


def test_expected_answers_are_not_sent_to_sentry() -> None:
    error = NotFoundError("No such workflow.")

    assert _scrub({"exception": {}}, {"exc_info": (type(error), error, None)}) is None
