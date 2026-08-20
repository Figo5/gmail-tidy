"""OAuth2 with the Gmail API.

Read-only scope by default; write scopes (gmail.modify + gmail.labels) are
requested only when apply/undo actually need them. Revoke removes the local
token after a best-effort server revoke; config and audit log are never touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from gmail_tidy.errors import AuthError

SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
SCOPE_LABELS = "https://www.googleapis.com/auth/gmail.labels"
SCOPE_WRITE = [SCOPE_MODIFY, SCOPE_LABELS]

TOKEN_NAME = "token.json"
SECRET_NAME = "client_secret.json"


def token_path(cfg: Path) -> Path:
    return cfg / TOKEN_NAME


def scope_state(token: Path) -> set[str]:
    if not token.exists():
        return set()
    try:
        data = json.loads(token.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return set(data.get("scopes", []))


def _chmod_600(path: Path) -> None:
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _save_token(cfg: Path, creds: Credentials, scopes: list[str]) -> None:
    token = token_path(cfg)
    payload = json.loads(creds.to_json())
    payload["scopes"] = list(scopes)
    token.write_text(json.dumps(payload), encoding="utf-8")
    _chmod_600(token)


def get_credentials(cfg: Path, client_secret: Path, require_write: bool = False) -> Credentials:
    scopes = SCOPE_WRITE if require_write else [SCOPE_READONLY]
    token = token_path(cfg)
    if token.exists():
        # A token file only proves *something* was granted before — not that it
        # covers what's being asked for now. `Credentials.valid` is scope-blind
        # (token present + not expired), and passing `scopes=` to
        # from_authorized_user_file stamps the requested scopes onto the loaded
        # object regardless of what was actually granted. So scope sufficiency
        # must be checked against the persisted `scopes` field ourselves before
        # trusting the cached token; otherwise a read-only token would be
        # silently reused for a write request instead of triggering re-consent.
        granted = scope_state(token)
        if set(scopes) <= granted:
            creds = Credentials.from_authorized_user_file(str(token), scopes=scopes)
            if creds and creds.valid:
                return creds
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                return creds
        token.unlink(missing_ok=True)  # corrupt/expired/insufficient scope; re-consent
    if not client_secret.exists():
        raise AuthError(
            f"missing {client_secret.name} in {cfg} — see docs/google-cloud-setup.md "
            "and run `gmail-tidy auth` to re-authenticate"
        )
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), scopes=scopes)
        creds = flow.run_local_server(port=0, prompt="consent")
    except Exception as exc:
        raise AuthError(f"OAuth consent failed — run `gmail-tidy auth` to retry: {exc}") from exc
    _save_token(cfg, creds, scopes)
    return creds


def upgrade_write(cfg: Path, client_secret: Path) -> Credentials:
    token = token_path(cfg)
    token.unlink(missing_ok=True)
    return get_credentials(cfg, client_secret, require_write=True)


def revoke(cfg: Path) -> None:
    token = token_path(cfg)
    if not token.exists():
        return
    try:
        creds = Credentials.from_authorized_user_file(str(token))
        if creds and creds.refresh_token:
            creds.revoke(Request())
    except Exception:
        pass  # server unreachable: token still removed locally; note server-side lifetime
    token.unlink(missing_ok=True)
