# tests/mock_gmail.py
"""In-memory double for the Gmail API. Only the allowed surface exists:
messages.list/get/batchModify, labels.list/get/create, users.getProfile.
Any other callable raises AttributeError — a second safety gate behind the AST test.

The mock models REAL Gmail label semantics: message ``labelIds`` and
``batchModify`` add/remove lists are **IDs** (opaque for user labels, identity
for system labels like INBOX/UNREAD), while ``labels.list`` returns
``{id, name}`` pairs. Setup helpers accept human-readable **names** and convert
internally, so tests read naturally while the wire format stays faithful.
"""

from dataclasses import dataclass, field

SYSTEM_LABELS = frozenset(
    {"INBOX", "UNREAD", "STARRED", "IMPORTANT", "SPAM", "TRASH", "DRAFT",
     "SENT", "CHAT"}
)


class _GError(Exception):
    def __init__(self, status: int, reason: str = "error"):
        self.status = status
        self.reason = reason
        super().__init__(f"HTTP {status}: {reason}")


@dataclass
class _Msg:
    id: str
    thread_id: str
    label_ids: set[str] = field(default_factory=set)
    internal_date_ms: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    size_kb: float = 0.0
    unread: bool = False


class _Req:
    """A pending request; execute() runs the handler (fail injection first)."""

    def __init__(self, api, method, kwargs):
        self._api = api
        self._method = method
        self._kwargs = kwargs

    def execute(self):
        if self._api.fail_before:
            self._api.fail_before(self._method)
        handler = self._api._handlers.get(self._method)
        if handler is None:
            raise AttributeError(f"forbidden Gmail API method: {self._method}")
        return handler(**self._kwargs)


class _Coll:
    """Resource collection, e.g. users().messages() or users().labels()."""

    def __init__(self, api, prefix: str):
        self._api = api
        self._prefix = prefix

    def __getattr__(self, name):
        method = f"{self._prefix}.{name}" if self._prefix else name
        return lambda **kw: _Req(self._api, method, kw)


class _Users:
    def __init__(self, api):
        self._api = api

    def messages(self):
        return _Coll(self._api, "")

    def labels(self):
        return _Coll(self._api, "labels")

    def getProfile(self, **kw):
        return _Req(self._api, "getProfile", kw)


class MockGmailApi:
    def __init__(self):
        self.store: dict[str, _Msg] = {}
        # name -> id for user labels; system labels are implicit (id == name)
        self._user_labels: dict[str, str] = {}
        self._id_counter = 0
        self.fail_before = None
        self._handlers = {
            "list": self._list,
            "get": self._get,
            "batchModify": self._batch_modify,
            "getProfile": self._get_profile,
            "labels.list": self._labels_list,
            "labels.get": self._labels_get,
            "labels.create": self._labels_create,
        }

    def users(self):
        return _Users(self)

    # --- setup helpers (accept NAMES, store IDs) ------------------------
    def add_message(self, msg_id: str, *, labels: set[str] | None = None,
                    size_kb: float = 0.0, subject: str = "",
                    from_hdr: str = "sender@example.com", to_hdr: str = "you@example.com",
                    unread: bool = False, internal_date_ms: int = 0) -> str:
        self.store[msg_id] = _Msg(
            id=msg_id,
            thread_id=f"t-{msg_id}",
            label_ids={self.label_id(l) for l in (labels or {"INBOX"})},
            internal_date_ms=internal_date_ms,
            headers={"From": from_hdr, "To": to_hdr, "Subject": subject},
            size_kb=size_kb,
            unread=unread,
        )
        return msg_id

    def label_id(self, name: str) -> str:
        """Gmail ID for a label name (system labels map to themselves)."""
        if name in SYSTEM_LABELS:
            return name
        if name not in self._user_labels:
            self._id_counter += 1
            self._user_labels[name] = f"Label_{self._id_counter}"
        return self._user_labels[name]

    def has_label(self, name: str) -> bool:
        """True if a user label exists on the account (never creates it)."""
        return name in self._user_labels

    def user_label_names(self) -> set[str]:
        """Names of all user labels currently on the account."""
        return set(self._user_labels)

    def label_name(self, label_id: str) -> str:
        """Name for a Gmail label ID (system labels map to themselves)."""
        for name, lid in self._user_labels.items():
            if lid == label_id:
                return name
        return label_id  # system label or unknown: id == name

    def label_ids_of(self, msg_id: str) -> set[str]:
        return set(self.store[msg_id].label_ids)

    def label_names_of(self, msg_id: str) -> set[str]:
        return {self.label_name(lid) for lid in self.store[msg_id].label_ids}

    # --- handlers -------------------------------------------------------
    def _list(self, **kw):
        query = kw.get("q", "")
        page_token = kw.get("pageToken")
        page_size = 2  # fixed small size forces pagination in tests
        msgs = [m for m in self.store.values() if _matches_query(m, query)]
        start = int(page_token) if page_token else 0
        chunk = msgs[start:start + page_size]
        result = {"messages": [{"id": m.id} for m in chunk]}
        if start + page_size < len(msgs):
            result["nextPageToken"] = str(start + page_size)
        return result

    def _get(self, **params):
        m = self.store[params["id"]]
        labels = set(m.label_ids)
        if m.unread:
            labels.add("UNREAD")
        return {
            "id": m.id,
            "threadId": m.thread_id,
            "labelIds": sorted(labels),
            "internalDate": str(m.internal_date_ms),
            "sizeEstimate": int(m.size_kb * 1024),  # matches real Gmail API field
            "payload": {"headers": [{"name": k, "value": v} for k, v in m.headers.items()]},
        }

    def _batch_modify(self, **kw):
        body = kw["body"]
        for msg_id in body["ids"]:
            m = self.store[msg_id]
            for label in body.get("addLabelIds", []):
                m.label_ids.add(label)
            for label in body.get("removeLabelIds", []):
                m.label_ids.discard(label)
        return {}

    def _get_profile(self, **kw):
        return {"emailAddress": "you@example.com"}

    def _labels_list(self, **kw):
        labels = [{"id": name, "name": name} for name in SYSTEM_LABELS]
        labels += [{"id": lid, "name": name} for name, lid in self._user_labels.items()]
        return {"labels": labels}

    def _labels_get(self, **kw):
        label_id = kw["id"]
        return {"id": label_id, "name": self.label_name(label_id)}

    def _labels_create(self, **kw):
        name = kw["body"]["name"]
        label_id = self.label_id(name)
        return {"id": label_id, "name": name}

    def __getattr__(self, name):
        raise AttributeError(f"forbidden Gmail API method: {name}")


def _matches_query(m: _Msg, query: str) -> bool:
    if not query:
        return True
    haystack = f"{m.headers.get('From', '')} {m.headers.get('Subject', '')}".lower()
    return all(part.lower() in haystack for part in query.split() if part)
