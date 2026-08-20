# tests/test_smoke.py
from gmail_tidy.errors import (
    EXIT_OK, EXIT_RUNTIME, EXIT_CONFIG, EXIT_NOOP,
    EXIT_AUTH, EXIT_CANCELLED, EXIT_PARTIAL,
    ConfigError, AuthError, NoWorkError, RequestError, PartialError, TidyError,
)

def test_exit_codes():
    assert (EXIT_OK, EXIT_RUNTIME, EXIT_CONFIG) == (0, 1, 2)
    assert (EXIT_NOOP, EXIT_AUTH, EXIT_CANCELLED, EXIT_PARTIAL) == (3, 4, 5, 6)

def test_error_hierarchy():
    for cls in (ConfigError, AuthError, NoWorkError, RequestError, PartialError):
        assert issubclass(cls, TidyError)
