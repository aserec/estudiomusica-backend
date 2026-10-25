from fastapi.testclient import TestClient


def test_docs_are_available(client: TestClient) -> None:
    response = client.get("/docs")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "swagger" in response.text.lower()


def test_openapi_documents_api_v1_errors_and_pagination(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    assert spec["info"]["title"] == "EstudioMusica API"
    assert spec["info"]["version"] == "0.1.0"
    assert "camelCase" in spec["info"]["description"]
    assert "pageSize" in spec["info"]["description"]

    paths = spec["paths"]
    assert "/api/v1/health" in paths
    assert "/api/v1/examples" in paths
    assert "/health" not in paths

    examples_get = paths["/api/v1/examples"]["get"]
    param_names = {param["name"] for param in examples_get["parameters"]}
    assert param_names == {"page", "pageSize"}

    schemas = spec["components"]["schemas"]
    assert "ErrorResponse" in schemas
    error_props = schemas["ErrorResponse"]["properties"]
    assert "error" in error_props
    assert "fields" in error_props
    assert "code" in error_props
