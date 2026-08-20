"""LabelIndex: canonical name <-> Gmail label ID mapping.

Gmail's API is inconsistent: message ``labelIds`` and ``batchModify``
``addLabelIds``/``removeLabelIds`` are **IDs**, while ``labels.list`` returns
``{id, name}`` pairs and config rules name labels by **name**. System labels
(INBOX, UNREAD, STARRED, ...) have ``id == name``; user labels get opaque IDs
(``Label_123...``).

This module is the single place that converts between the two. Internally the
rest of gmail-tidy works with canonical **names**; IDs are resolved only at the
Gmail write boundary.
"""

from __future__ import annotations

# System labels whose Gmail id equals their name. Kept as a frozenset so the
# identity mapping is explicit and testable.
SYSTEM_LABELS = frozenset(
    {"INBOX", "UNREAD", "STARRED", "IMPORTANT", "SPAM", "TRASH", "DRAFT",
     "SENT", "CHAT"}
)


class LabelIndex:
    """Bidirectional name <-> id index over the account's labels.

    System labels are always present and map to themselves. User labels are
    added from ``labels.list`` results or via :meth:`add`.
    """

    def __init__(self) -> None:
        self._name_to_id: dict[str, str] = {n: n for n in SYSTEM_LABELS}
        self._id_to_name: dict[str, str] = {n: n for n in SYSTEM_LABELS}

    @classmethod
    def from_labels(cls, labels: list[dict]) -> "LabelIndex":
        """Build an index from a ``labels.list`` payload (list of {id, name})."""
        idx = cls()
        for lbl in labels:
            idx.add(lbl["name"], lbl["id"])
        return idx

    def add(self, name: str, label_id: str) -> None:
        """Register a user label. Idempotent for the same name/id pair."""
        self._name_to_id[name] = label_id
        self._id_to_name[label_id] = name

    def name_to_id(self, name: str) -> str | None:
        """Resolve a canonical name to a Gmail label ID, or None if unknown."""
        return self._name_to_id.get(name)

    def id_to_name(self, label_id: str) -> str | None:
        """Resolve a Gmail label ID to a canonical name, or None if unknown."""
        return self._id_to_name.get(label_id)

    def resolve_name(self, name: str) -> str | None:
        """Alias of :meth:`name_to_id` (read boundary: name -> ID)."""
        return self.name_to_id(name)

    def resolve_id(self, label_id: str) -> str | None:
        """Alias of :meth:`id_to_name` (write boundary: ID -> name)."""
        return self.id_to_name(label_id)

    def names(self) -> set[str]:
        return set(self._name_to_id)

    def ids(self) -> set[str]:
        return set(self._id_to_name)
