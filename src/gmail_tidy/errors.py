"""Exit codes and error hierarchy. Exit codes are stable, public API."""

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2
EXIT_NOOP = 3
EXIT_AUTH = 4
EXIT_CANCELLED = 5
EXIT_PARTIAL = 6


class TidyError(Exception):
    """Base class for all gmail-tidy errors."""


class ConfigError(TidyError):
    """Invalid configuration (maps to EXIT_CONFIG)."""


class AuthError(TidyError):
    """Authentication/authorization failure (maps to EXIT_AUTH)."""


class NoWorkError(TidyError):
    """Nothing to do (maps to EXIT_NOOP)."""


class RequestError(TidyError):
    """Gmail API request failed after retries (maps to EXIT_RUNTIME)."""


class PartialError(TidyError):
    """Some batches failed; resume with apply (maps to EXIT_PARTIAL)."""
