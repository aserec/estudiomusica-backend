from fastapi.testclient import TestClient


def test_pydantic_validation_returns_400_validation_fields(
    client: TestClient,
) -> None:
    response = client.post("/api/v1/examples", json={"displayName": ""})

    assert response.status_code == 400
    assert response.json() == {"error": "validation", "fields": ["displayName"]}


def test_not_found_returns_404_not_found(client: TestClient) -> None:
    response = client.get("/api/v1/examples/missing")

    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}


def test_access_denied_returns_403_forbidden(client: TestClient) -> None:
    response = client.get("/api/v1/examples/denied")

    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}


def test_payload_too_large_returns_413(client: TestClient) -> None:
    response = client.get("/api/v1/examples/too-large")

    assert response.status_code == 413
    assert response.json() == {"error": "payload_too_large"}


def test_unknown_path_uses_not_found_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}


def test_http_exception_slug_becomes_error_code(probe_client: TestClient) -> None:
    response = probe_client.get("/api/v1/_test/errors/not_open")

    assert response.status_code == 403
    assert response.json() == {"error": "not_open"}


def test_json_keys_are_camel_case_on_success(client: TestClient) -> None:
    response = client.get("/api/v1/examples/ex-1")

    assert response.status_code == 200
    assert response.json() == {"id": "ex-1", "displayName": "Ejemplo 1"}
    assert response.headers["x-api-version"] == "v1"


def test_put_updates_example(client: TestClient) -> None:
    response = client.put(
        "/api/v1/examples/ex-1",
        json={"displayName": "Nombre nuevo"},
    )

    assert response.status_code == 200
    assert response.json() == {"id": "ex-1", "displayName": "Nombre nuevo"}
