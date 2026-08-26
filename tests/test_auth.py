# tests/test_auth.py
from pathlib import Path
import pytest
from gmail_tidy.auth import get_credentials, scope_state, revoke, token_path, SCOPE_READONLY
from gmail_tidy.errors import AuthError


def test_scope_state_reads_metadata(tmp_path):
    tok = tmp_path / "token.json"
    tok.write_text('{"scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}',
                   encoding="utf-8")
    assert scope_state(tok) == {SCOPE_READONLY}


def test_scope_state_empty_when_missing(tmp_path):
    assert scope_state(tmp_path / "nope.json") == set()


def test_revoke_removes_local_files_never_audit(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "token.json").write_text("{}", encoding="utf-8")
    (cfg / "audit.jsonl").write_text("x\n", encoding="utf-8")
    (cfg / "config.yaml").write_text("account: x", encoding="utf-8")
    revoke(cfg)
    assert not (cfg / "token.json").exists()
    assert (cfg / "audit.jsonl").exists()
    assert (cfg / "config.yaml").exists()


def test_token_path_constant(tmp_path):
    assert token_path(tmp_path) == tmp_path / "token.json"


def _write_scope_file(tmp_path, text: str) -> Path:
    """Write token.json at tmp_path and return its path."""
    tok = tmp_path / "token.json"
    tok.write_text(text, encoding="utf-8")
    return tok


# --- valid-JSON wrong-shape token data (Task 37) -----------------------------
# scope_state must degrade exactly like a missing/corrupt file: valid JSON that
# is the wrong shape (top-level non-object, `scopes` not a list, or a list
# containing ANY non-string) returns an empty set — never a raw
# AttributeError/TypeError leaking through status/auth status/the headless gate.


def test_scope_state_top_level_list_returns_empty(tmp_path):
    assert scope_state(_write_scope_file(tmp_path, "[1, 2, 3]")) == set()


def test_scope_state_top_level_string_returns_empty(tmp_path):
    assert scope_state(_write_scope_file(tmp_path, '"hello"')) == set()


def test_scope_state_top_level_number_returns_empty(tmp_path):
    assert scope_state(_write_scope_file(tmp_path, "42")) == set()


def test_scope_state_scopes_string_returns_empty(tmp_path):
    """`scopes` as a string is a shape error — never a character soup."""
    assert scope_state(_write_scope_file(tmp_path, '{"scopes": "readonly"}')) == set()


def test_scope_state_scopes_number_returns_empty(tmp_path):
    assert scope_state(_write_scope_file(tmp_path, '{"scopes": 42}')) == set()


def test_scope_state_scopes_object_returns_empty(tmp_path):
    assert scope_state(_write_scope_file(tmp_path, '{"scopes": {"a": 1}}')) == set()


def test_scope_state_scopes_null_returns_empty(tmp_path):
    assert scope_state(_write_scope_file(tmp_path, '{"scopes": null}')) == set()


def test_scope_state_mixed_scopes_returns_empty(tmp_path):
    """A list containing ANY non-string is a shape error — the whole set is empty."""
    assert scope_state(_write_scope_file(
        tmp_path,
        '{"scopes": ["https://www.googleapis.com/auth/gmail.readonly", 42]}',
    )) == set()


def test_scope_state_all_non_string_scopes_returns_empty(tmp_path):
    assert scope_state(_write_scope_file(tmp_path, '{"scopes": [42, true, null]}')) == set()


def test_scope_state_missing_scopes_key_returns_empty(tmp_path):
    assert scope_state(_write_scope_file(tmp_path, '{"token": "x"}')) == set()


def test_scope_state_valid_empty_list_returns_empty(tmp_path):
    assert scope_state(_write_scope_file(tmp_path, '{"scopes": []}')) == set()


def test_scope_state_corrupt_json_returns_empty(tmp_path):
    assert scope_state(_write_scope_file(tmp_path, "{ not json")) == set()


# --- invalid UTF-8 bytes (Task 40) -------------------------------------------
# A token.json containing bytes that do not decode as UTF-8 (e.g. a truncated
# write, or a file half-overwritten by another program) must degrade exactly
# like a missing/corrupt file: an empty set — never a raw UnicodeDecodeError
# leaking through status/auth status/the headless write-scope gate.


def test_scope_state_invalid_utf8_bytes_returns_empty(tmp_path):
    assert scope_state(_write_scope_file(tmp_path, "\xff\xfe\x00\x00")) == set()


def test_readonly_token_is_not_silently_reused_for_write(tmp_path):
    """A read-only token must never be returned for a require_write=True call.

    No network/live consent is exercised here: with no client_secret.json
    present, insufficient-scope must surface as AuthError (forcing the caller
    to run `gmail-tidy auth`) rather than as a silently-returned stale token.
    """
    tok = tmp_path / "token.json"
    tok.write_text(
        '{"token": "fake", "refresh_token": "fake", "client_id": "x", '
        '"client_secret": "x", "token_uri": "https://oauth2.googleapis.com/token", '
        '"scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}',
        encoding="utf-8",
    )
    with pytest.raises(AuthError):
        get_credentials(tmp_path, tmp_path / "client_secret.json", require_write=True)
    # the insufficient-scope token must be discarded, not left in place silently
    assert not tok.exists()
