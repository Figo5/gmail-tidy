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
