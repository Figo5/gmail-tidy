"""Undo reverses a run's label/archive actions, skipping user-changed messages.

The safety rule (spec §11): a message is only touched if its current label set
exactly equals the state the run left behind. If the user changed anything since,
the message is skipped — newer user state is never clobbered.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from gmail_tidy.audit import AuditEntry, AuditLog, Candidate
from gmail_tidy.errors import EXIT_CANCELLED, EXIT_OK
from gmail_tidy.gmail_client import GmailClient


@dataclass
class InverseAction:
    message_id: str
    thread_id: str
    rule_id: str
    add_label: list[str] = field(default_factory=list)
    remove_label: list[str] = field(default_factory=list)
    re_inbox: bool = False
    expected_labels: set[str] = field(default_factory=set)


def expected_after(cand: Candidate) -> set[str]:
    """Labels the run should have left behind."""
    removed = set(cand.actions.remove_label)
    if cand.actions.archive:
        removed.add("INBOX")
    return (set(cand.before_labels) | set(cand.actions.add_label)) - removed


def build_undo_plan(cand: Candidate) -> list[InverseAction]:
    """Invert the run: re-add what it removed, remove what it added, re-inbox."""
    return [
        InverseAction(
            message_id=cand.message_id,
            thread_id=cand.thread_id,
            rule_id=cand.rule_id,
            add_label=list(cand.actions.remove_label),
            remove_label=list(cand.actions.add_label),
            re_inbox=cand.actions.archive and cand.in_inbox,
            expected_labels=expected_after(cand),
        )
    ]


def execute_undo(client: GmailClient, plan: list[InverseAction], audit: AuditLog,
                 run_id: str, confirm: Callable[[], bool]) -> int:
    if not confirm():
        return EXIT_CANCELLED
    # One label index for the whole undo. Undo never creates labels: any name
    # that cannot be resolved to an existing Gmail label ID is skipped.
    index = client.fetch_label_index()
    for inv in plan:
        meta = client.get_meta(inv.message_id, index)
        if set(meta.labels) != inv.expected_labels:
            continue  # user changed the message; never clobber
        # Write boundary: resolve canonical names to Gmail label IDs.
        add_ids: list[str] = []
        for name in list(inv.add_label) + (["INBOX"] if inv.re_inbox else []):
            label_id = index.name_to_id(name)
            if label_id is not None:
                add_ids.append(label_id)
        remove_ids: list[str] = []
        for name in inv.remove_label:
            label_id = index.name_to_id(name)
            if label_id is not None:
                remove_ids.append(label_id)
        if not add_ids and not remove_ids:
            continue
        client.batch_modify([meta.id], add=add_ids, remove=remove_ids)
        for label in list(inv.add_label) + (["INBOX"] if inv.re_inbox else []):
            audit.append(AuditEntry(run_id=run_id, message_id=meta.id, thread_id=meta.thread_id,
                                    rule_id=inv.rule_id, action="add_label", payload=label,
                                    kind="undo"))
        for label in inv.remove_label:
            audit.append(AuditEntry(run_id=run_id, message_id=meta.id, thread_id=meta.thread_id,
                                    rule_id=inv.rule_id, action="remove_label", payload=label,
                                    kind="undo"))
    return EXIT_OK
