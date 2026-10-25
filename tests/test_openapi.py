from typing import Any, cast

from fastapi.testclient import TestClient


def test_docs_are_available(client: TestClient) -> None:
    response = client.get("/docs")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "swagger" in response.text.lower()


def test_openapi_is_published_with_api_version(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    assert spec["info"]["title"] == "EstudioMusica API"
    assert spec["info"]["version"] == client.app.version
    assert "/api/v1" in spec["info"]["description"]
    assert "X-API-Version" in spec["info"]["description"]
    assert not spec.get("servers")


def test_openapi_documents_errors_and_pagination(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    paths = spec["paths"]
    assert "/api/v1/health" in paths
    assert "/api/v1/examples" in paths
    assert "/api/v1/examples/denied" in paths
    assert "/api/v1/examples/too-large" in paths
    assert "/health" not in paths

    examples_get = paths["/api/v1/examples"]["get"]
    param_names = {param["name"] for param in examples_get["parameters"]}
    assert param_names == {"page", "pageSize"}
    page_size = next(
        param for param in examples_get["parameters"] if param["name"] == "pageSize"
    )
    page_size_schema = page_size.get("schema", page_size)
    assert "maximum" not in page_size_schema

    schemas = spec["components"]["schemas"]
    assert "ErrorResponse" in schemas
    assert "HTTPValidationError" not in schemas
    assert "$defs" not in schemas["ErrorResponse"]
    error_props = schemas["ErrorResponse"]["properties"]
    assert set(error_props) >= {"error", "code", "fields"}
    fields_schema = error_props["fields"]
    items = fields_schema.get("items") or fields_schema.get("anyOf", [{}])[0].get(
        "items"
    )
    assert items is not None
    assert items.get("type") == "string"

    for path_item in paths.values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            responses = cast(dict[str, Any], operation.get("responses") or {})
            assert "422" not in responses

    post_responses = paths["/api/v1/examples"]["post"]["responses"]
    assert "400" in post_responses
    assert post_responses["400"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
