from fastapi.testclient import TestClient

ALLOWED_ORIGIN = "http://localhost:3000"
UNKNOWN_ORIGIN = "https://evil.example"


def test_cors_allows_configured_origin_with_credentials(client: TestClient) -> None:
    response = client.get(
        "/api/v1/health",
        headers={"Origin": ALLOWED_ORIGIN},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


def test_cors_preflight_for_api_v1_with_credentials(client: TestClient) -> None:
    response = client.options(
        "/api/v1/examples",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization,Content-Type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    allow_methods = response.headers["access-control-allow-methods"]
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        assert method in allow_methods


def test_cors_rejects_unknown_origin_with_credentials(client: TestClient) -> None:
    response = client.get(
        "/api/v1/health",
        headers={"Origin": UNKNOWN_ORIGIN},
    )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
