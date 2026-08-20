"""Thin, retry-capable wrapper over the Gmail API.

Only the allowed surface is used: list/get/batchModify on messages,
list/get/create on labels, getProfile on users.
"""

from __future__ import annotations

import time

from gmail_tidy.errors import AuthError, RequestError
from gmail_tidy.rules import MessageMeta

PAGE_SIZE = 100
BATCH_SIZE = 1000
MAX_RETRIES = 3
BACKOFF_BASE = 2.0
BACKOFF_CAP = 60.0


def chunked(items: list[str], size: int = BATCH_SIZE) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


class GmailClient:
    def __init__(self, service, page_size: int = PAGE_SIZE):
        self._svc = service
        self._page_size = page_size

    def _execute(self, request, method: str):
        attempt = 0
        while True:
            try:
                return request.execute()
            except Exception as exc:
                status = getattr(exc, "status", None)
                if status in (429, 500, 503) and attempt < MAX_RETRIES:
                    attempt += 1
                    delay = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt))
                    time.sleep(delay)
                    continue
                if status == 403:
                    raise AuthError(
                        "Gmail access denied (403) — run `gmail-tidy auth` to re-authenticate"
                    ) from exc
                raise RequestError(f"Gmail request failed (status={status}) in {method}") from exc

    def list(self, query: str = "", limit: int | None = None) -> list[str]:
        out: list[str] = []
        page_token = None
        page_size = min(self._page_size, limit) if limit else self._page_size
        while True:
            params = {"userId": "me", "maxResults": page_size}
            if query:
                params["q"] = query
            if page_token:
                params["pageToken"] = page_token
            data = self._execute(self._svc.users().messages().list(**params), "list")
            out.extend(m["id"] for m in data.get("messages", []))
            if limit is not None and len(out) >= limit:
                return out[:limit]
            page_token = data.get("nextPageToken")
            if not page_token:
                return out

    def get_meta(self, msg_id: str) -> MessageMeta:
        data = self._execute(
            self._svc.users().messages().get(userId="me", id=msg_id, format="metadata"),
            "get",
        )
        headers = {h["name"]: h["value"] for h in data.get("payload", {}).get("headers", [])}
        return MessageMeta(
            id=data["id"],
            thread_id=data.get("threadId", data["id"]),
            labels=set(data.get("labelIds", [])),
            internal_date_ms=int(data.get("internalDate", "0")),
            from_header=headers.get("From"),
            to_header=headers.get("To"),
            subject_header=headers.get("Subject"),
            size_kb=data.get("sizeEstimate", 0) / 1024.0,
            unread="UNREAD" in data.get("labelIds", []),
        )

    def batch_modify(self, ids: list[str], add: list[str], remove: list[str]) -> None:
        for chunk in chunked(ids):
            body: dict = {"ids": chunk}
            if add:
                body["addLabelIds"] = add
            if remove:
                body["removeLabelIds"] = remove
            self._execute(
                self._svc.users().messages().batchModify(userId="me", body=body),
                "batchModify",
            )

    def list_label_names(self) -> list[str]:
        data = self._execute(self._svc.users().labels().list(userId="me"), "labels.list")
        return [lbl["name"] for lbl in data.get("labels", [])]

    def ensure_label(self, name: str) -> str:
        data = self._execute(self._svc.users().labels().list(userId="me"), "labels.list")
        for lbl in data.get("labels", []):
            if lbl["name"] == name:
                return lbl["id"]
        created = self._execute(
            self._svc.users().labels().create(userId="me", body={"name": name}),
            "labels.create",
        )
        return created["id"]

    def profile_email(self) -> str:
        data = self._execute(self._svc.users().getProfile(userId="me"), "getProfile")
        return data.get("emailAddress", "")
