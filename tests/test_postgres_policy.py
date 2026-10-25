import errno
import socket

from tests.conftest import _is_connection_error


def test_connection_refused_is_connection_error() -> None:
    assert _is_connection_error(ConnectionRefusedError("Connection refused"))


def test_name_resolution_is_connection_error() -> None:
    assert _is_connection_error(socket.gaierror(-2, "Name or service not known"))


def test_timeout_is_connection_error() -> None:
    assert _is_connection_error(TimeoutError())
    assert _is_connection_error(TimeoutError())


def test_oserror_refused_is_connection_error() -> None:
    assert _is_connection_error(OSError(errno.ECONNREFUSED, "Connection refused"))


def test_password_error_is_not_connection_error() -> None:
    exc = RuntimeError('password authentication failed for user "estudiomusica"')
    assert _is_connection_error(exc) is False


def test_invalid_catalog_is_not_connection_error() -> None:
    exc = RuntimeError('database "estudiomusica_test" does not exist')
    assert _is_connection_error(exc) is False


def test_wrapped_refused_is_connection_error() -> None:
    inner = ConnectionRefusedError("Connection refused")
    outer = RuntimeError("connect failed")
    outer.__cause__ = inner
    assert _is_connection_error(outer)
