from fastapi.testclient import TestClient


def test_health_ok_when_dependencies_answer(client: TestClient, all_dependencies_up: None) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "ORM_AI"
    assert body["environment"] == "test"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["redis"]["status"] == "ok"


def test_health_degraded_when_redis_is_down(client: TestClient, redis_down: None) -> None:
    response = client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["redis"] == {
        "status": "error",
        "latency_ms": body["checks"]["redis"]["latency_ms"],
        "error": "ConnectionError",
    }


def test_health_never_leaks_connection_details(client: TestClient, redis_down: None) -> None:
    body = client.get("/api/health").json()

    assert "refused" not in body["checks"]["redis"]["error"]
    assert "localhost" not in str(body)
