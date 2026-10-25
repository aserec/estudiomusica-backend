from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["content-type"].startswith("application/json")


def test_health_is_not_at_root(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"
