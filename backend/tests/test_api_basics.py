from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_every_response_has_a_request_id(client: TestClient, all_dependencies_up: None) -> None:
    response = client.get("/api/health")

    assert len(response.headers["X-Request-ID"]) == 32


def test_valid_incoming_request_id_is_kept(client: TestClient, all_dependencies_up: None) -> None:
    response = client.get("/api/health", headers={"X-Request-ID": "shop-42.req_1"})

    assert response.headers["X-Request-ID"] == "shop-42.req_1"


def test_unsafe_incoming_request_id_is_replaced(
    client: TestClient, all_dependencies_up: None
) -> None:
    response = client.get("/api/health", headers={"X-Request-ID": "x" * 200})

    assert response.headers["X-Request-ID"] != "x" * 200
    assert len(response.headers["X-Request-ID"]) == 32


def test_unknown_route_uses_the_error_envelope(client: TestClient) -> None:
    response = client.get("/api/does-not-exist")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"] == response.headers["X-Request-ID"]


def test_unhandled_error_returns_500_without_internals(app: FastAPI) -> None:
    @app.get("/api/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/boom")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert "secret" not in error["message"]
    assert error["request_id"]


def test_cors_allows_the_frontend_origin(client: TestClient, all_dependencies_up: None) -> None:
    response = client.get("/api/health", headers={"Origin": "http://localhost:3000"})

    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
