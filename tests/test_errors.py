from fastapi.testclient import TestClient


def test_validation_error_uses_frontend_envelope(client: TestClient) -> None:
    response = client.post("/api/v1/examples", json={"displayName": ""})

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "validation_error"
    assert "_" not in "".join(body.keys())
    assert isinstance(body["fields"], list)
    assert body["fields"][0]["field"] == "displayName"
    assert "message" in body["fields"][0]


def test_not_found_uses_snake_case_error_code(client: TestClient) -> None:
    response = client.get("/api/v1/examples/missing")

    assert response.status_code == 404
    body = response.json()
    assert body == {
        "error": "not_found",
        "message": "No existe el ejemplo.",
    }


def test_http_exception_detail_becomes_error_code(probe_client: TestClient) -> None:
    response = probe_client.get("/api/v1/_test/errors/not_open")

    assert response.status_code == 403
    assert response.json() == {"error": "not_open"}


def test_error_codes_stay_snake_case(probe_client: TestClient) -> None:
    response = probe_client.get(
        "/api/v1/_test/errors/invalid_score",
        params={"status_code": 400},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_score"


def test_json_keys_are_camel_case_on_success(client: TestClient) -> None:
    response = client.get("/api/v1/examples/ex-1")

    assert response.status_code == 200
    assert response.json() == {"id": "ex-1", "displayName": "Ejemplo 1"}


def test_put_updates_example(client: TestClient) -> None:
    response = client.put(
        "/api/v1/examples/ex-1",
        json={"displayName": "Nombre nuevo"},
    )

    assert response.status_code == 200
    assert response.json() == {"id": "ex-1", "displayName": "Nombre nuevo"}
