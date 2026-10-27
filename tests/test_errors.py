import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app.exceptions import pg_sqlstate


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


def test_http_exception_400_text_is_bad_request_without_fields(
    probe_client: TestClient,
) -> None:
    response = probe_client.get("/api/v1/_test/http-400-text")

    assert response.status_code == 400
    assert response.json() == {"error": "bad_request"}


def test_http_exception_422_becomes_400(probe_client: TestClient) -> None:
    response = probe_client.get("/api/v1/_test/http-422")

    assert response.status_code == 400
    assert response.json()["error"] == "validation"


def test_http_exception_detail_message_hidden_in_production(
    offline_production_client: TestClient,
) -> None:
    response = offline_production_client.get("/api/v1/_test/http-400-dict")

    assert response.status_code == 400
    body = response.json()
    assert body == {"error": "invalid_evaluation", "code": "no_solution"}
    assert "secret-leak" not in response.text


def test_malformed_json_fields_are_body_not_index(client: TestClient) -> None:
    response = client.post(
        "/api/v1/examples",
        content=b"{not-json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "validation", "fields": ["body"]}


def test_client_supplied_field_names_are_replaced_by_parent(
    probe_client: TestClient,
) -> None:
    response = probe_client.post(
        "/api/v1/_test/loose",
        json={"secretKey": "nope", "__proto__": "nope", "ok_field": "nope"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "validation", "fields": ["body"]}
    assert "secretKey" not in response.text
    assert "__proto__" not in response.text
    assert "ok_field" not in response.text
    assert "okField" not in response.text


def test_declared_model_fields_keep_camel_case_alias(client: TestClient) -> None:
    response = client.post("/api/v1/examples", json={"displayName": ""})

    assert response.status_code == 400
    assert response.json() == {"error": "validation", "fields": ["displayName"]}


def test_http_418_uses_http_status_slug(probe_client: TestClient) -> None:
    response = probe_client.get("/api/v1/_test/http-418")

    assert response.status_code == 418
    assert response.json() == {"error": "im_a_teapot"}


def test_domain_validation_stays_400_not_422(probe_client: TestClient) -> None:
    response = probe_client.get("/api/v1/_test/validation")

    assert response.status_code == 400
    assert response.status_code != 422
    assert response.json() == {"error": "validation", "fields": ["email"]}


def test_pg_sqlstate_reads_orig_not_cause() -> None:
    class _PgError(Exception):
        sqlstate = "40P01"

    class _Cause(Exception):
        sqlstate = "40001"

    wrapped = DBAPIError("SELECT 1", {}, _PgError())
    assert pg_sqlstate(wrapped) == "40P01"
    orphan = Exception("plain")
    orphan.__cause__ = _Cause()
    assert pg_sqlstate(orphan) == ""


def test_pg_deadlock_and_serialization_are_503_not_500(
    probe_client: TestClient,
) -> None:
    deadlock = probe_client.get("/api/v1/_test/pg-deadlock")
    assert deadlock.status_code == 503
    assert deadlock.json() == {"error": "transient_db_conflict"}
    assert deadlock.headers.get("retry-after") == "1"
    serial = probe_client.get("/api/v1/_test/pg-serialization")
    assert serial.status_code == 503
    assert serial.json() == {"error": "transient_db_conflict"}
    assert serial.headers.get("retry-after") == "1"
    raw_deadlock = probe_client.get("/api/v1/_test/pg-raw-deadlock")
    assert raw_deadlock.status_code == 503
    assert raw_deadlock.json() == {"error": "transient_db_conflict"}
    assert raw_deadlock.headers.get("retry-after") == "1"
    raw_serial = probe_client.get("/api/v1/_test/pg-raw-serialization")
    assert raw_serial.status_code == 503
    assert raw_serial.json() == {"error": "transient_db_conflict"}
    assert raw_serial.headers.get("retry-after") == "1"


def test_transient_retry_succeeds_and_exhaust_is_503(probe_client: TestClient) -> None:
    ok = probe_client.get("/api/v1/_test/pg-transient-retry")
    assert ok.status_code == 200
    assert ok.json() == {"attempts": 2}
    exhausted = probe_client.get("/api/v1/_test/pg-transient-exhaust")
    assert exhausted.status_code == 503
    assert exhausted.json() == {"error": "transient_db_conflict"}
    assert exhausted.headers.get("retry-after") == "1"


def test_unhandled_error_is_500_without_message(client: TestClient) -> None:
    response = client.get("/api/v1/_test/boom")

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}
    assert "secret-leak" not in response.text
    assert response.headers["x-api-version"] == "v1"
    assert b": " not in response.content


def test_500_logs_traceback(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="app.middleware"):
        response = client.get("/api/v1/_test/boom")

    assert response.status_code == 500
    assert "RuntimeError" in caplog.text
    assert "secret-leak" in caplog.text


def test_mid_stream_exception_is_reraised(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level(logging.ERROR, logger="app.middleware"),
        pytest.raises(RuntimeError, match="mid-stream"),
    ):
        client.get("/api/v1/_test/stream-boom")
    assert "mid-stream" in caplog.text


def test_offline_production_client_is_production(
    offline_production_client: TestClient,
) -> None:
    from fastapi import Response

    from app.auth.cookies import attach_session_cookie
    from app.config import get_settings

    settings = get_settings()
    assert settings.app_env == "production"
    assert settings.auth_cookie_secure() is True
    assert settings.expose_errors is False
    assert settings.example_routes_enabled() is False
    response = Response()
    attach_session_cookie(response, "dummy-token", settings)
    cookie = str(response.headers.get("set-cookie", ""))
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()


def test_production_500_has_no_message(
    offline_production_client: TestClient,
) -> None:
    response = offline_production_client.get("/api/v1/_test/boom")

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}
    assert "message" not in response.json()
    assert "secret-leak" not in response.text


def test_expose_errors_opt_in_includes_message(
    expose_errors_client: TestClient,
) -> None:
    response = expose_errors_client.get("/api/v1/_test/boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": "internal_error",
        "message": "secret-leak",
    }


def test_unhandled_error_includes_cors_and_version(client: TestClient) -> None:
    response = client.get(
        "/api/v1/_test/boom",
        headers={"Origin": "http://localhost:3000"},
    )

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}
    assert response.headers["x-api-version"] == "v1"
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_version_header_is_not_limited_to_2xx(client: TestClient) -> None:
    not_found = client.get("/api/v1/does-not-exist")
    boom = client.get("/api/v1/_test/boom")

    assert not_found.status_code == 404
    assert boom.status_code == 500
    assert not_found.headers["x-api-version"] == "v1"
    assert boom.headers["x-api-version"] == "v1"


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


def test_examples_are_disabled_in_production(
    offline_production_client: TestClient,
) -> None:
    response = offline_production_client.get("/api/v1/examples")

    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}
