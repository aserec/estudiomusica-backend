from fastapi.testclient import TestClient


def test_pagination_defaults_to_first_page_of_20(client: TestClient) -> None:
    response = client.get("/api/v1/examples")

    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["pageSize"] == 20
    assert body["total"] == 25
    assert len(body["items"]) == 20
    assert body["items"][0]["id"] == "ex-1"
    assert "page_size" not in body


def test_pagination_honors_page_and_page_size(client: TestClient) -> None:
    response = client.get("/api/v1/examples", params={"page": 2, "pageSize": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 2
    assert body["pageSize"] == 10
    assert body["total"] == 25
    assert [item["id"] for item in body["items"]] == [f"ex-{n}" for n in range(11, 21)]


def test_pagination_empty_page_is_200(client: TestClient) -> None:
    response = client.get("/api/v1/examples", params={"page": 99, "pageSize": 20})

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["page"] == 99
    assert body["pageSize"] == 20
    assert body["total"] == 25


def test_invalid_pagination_params_return_validation_error(client: TestClient) -> None:
    response = client.get("/api/v1/examples", params={"page": 0, "pageSize": 0})

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "validation"
    assert set(body["fields"]) == {"page", "pageSize"}
