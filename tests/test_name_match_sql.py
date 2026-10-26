from sqlalchemy.dialects import postgresql

from app.services.users import _name_match


def test_name_match_compiles_without_concat() -> None:
    compiled = _name_match("ana").compile(
        dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
    )
    sql = str(compiled)
    assert "concat(" not in sql.lower()
    assert "users.first_name || ' ' || users.last_name" in sql
    assert sql == (
        "normalize(users.first_name || ' ' || users.last_name, NFC) "
        "ILIKE %(normalize_1)s::VARCHAR ESCAPE '\\'"
    )
    assert list(compiled.params.values()) == ["%ana%"]
