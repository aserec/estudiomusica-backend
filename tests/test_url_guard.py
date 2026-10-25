import pytest

from tests.conftest import (
    derive_alembic_database_url,
    require_test_database_url,
    resolve_alembic_database_url,
    resolve_test_database_url,
)

_SECRET = "guard-secret-must-not-appear"


def _dsn(path: str, query: str = "") -> str:
    suffix = f"?{query}" if query else ""
    return f"postgresql+asyncpg://estudiomusica:{_SECRET}@127.0.0.1:5432/{path}{suffix}"


def test_require_test_database_url_accepts_test_name() -> None:
    url = _dsn("estudiomusica_test")
    assert require_test_database_url(url) == url


def test_require_test_database_url_rejects_dev_name() -> None:
    with pytest.raises(pytest.UsageError, match="_test") as caught:
        require_test_database_url(_dsn("estudiomusica"))
    assert _SECRET not in str(caught.value)
    assert "***" in str(caught.value)


@pytest.mark.parametrize(
    "query",
    [
        "database=estudiomusica",
        "dbname=estudiomusica",
        "Database=estudiomusica",
        "service=dev",
        "options=-c%20search_path=public",
        "ssl=require&database=estudiomusica",
    ],
)
def test_require_test_database_url_rejects_override_query(query: str) -> None:
    with pytest.raises(pytest.UsageError, match="parámetros") as caught:
        require_test_database_url(_dsn("estudiomusica_test", query))
    assert _SECRET not in str(caught.value)


def test_require_test_database_url_allows_harmless_query() -> None:
    url = _dsn("estudiomusica_test", "ssl=require&application_name=pytest")
    assert require_test_database_url(url) == url


def test_resolve_test_database_url_rejects_query_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        _dsn("estudiomusica_test", "database=estudiomusica"),
    )
    with pytest.raises(pytest.UsageError, match="parámetros"):
        resolve_test_database_url()


def test_derive_alembic_database_url_from_test_name() -> None:
    from sqlalchemy.engine.url import make_url

    derived = derive_alembic_database_url(_dsn("estudiomusica_test"))
    parsed = make_url(derived)
    assert parsed.database == "estudiomusica_alembic_test"
    assert parsed.password == _SECRET
    require_test_database_url(derived, env_name="ALEMBIC_TEST_DATABASE_URL")


def test_resolve_alembic_database_url_derives_from_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", _dsn("app_test"))
    monkeypatch.delenv("ALEMBIC_TEST_DATABASE_URL", raising=False)
    url = resolve_alembic_database_url()
    assert url.endswith("/app_alembic_test")


def test_resolve_alembic_database_url_rejects_explicit_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALEMBIC_TEST_DATABASE_URL", _dsn("estudiomusica"))
    with pytest.raises(pytest.UsageError, match="ALEMBIC_TEST_DATABASE_URL") as caught:
        resolve_alembic_database_url()
    assert _SECRET not in str(caught.value)
