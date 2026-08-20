"""Planning (scan) and the only write path (apply). Reconcile-before-apply."""

from __future__ import annotations

from collections.abc import Callable

from gmail_tidy.audit import AuditEntry, AuditLog, Candidate, RunJournal
from gmail_tidy.config import Actions, Config, MatchConfig
from gmail_tidy.errors import EXIT_CANCELLED, EXIT_OK, EXIT_PARTIAL
from gmail_tidy.gmail_client import GmailClient
from gmail_tidy.rules import MessageMeta, first_matching_rule, is_excluded


def query_from_match(match: MatchConfig) -> str:
    """Best-effort Gmail search narrowing (bare terms). Never the source of truth.

    Eligibility is always re-decided locally by first_matching_rule against
    fetched metadata; this query only narrows what gets fetched. Bare terms
    (no operator syntax) are used deliberately so narrowing degrades safely
    to "fetch more, filter locally" rather than depending on exact operator
    support from whatever is on the other end of GmailClient.list().
    """
    parts: list[str] = []
    parts.extend(match.subject_contains)
    parts.extend(match.from_contains)
    if match.category:
        parts.append(match.category)
    return " ".join(parts)


def noop_eliminate(meta: MessageMeta, actions: Actions) -> tuple[Actions, bool]:
    """Drop actions already satisfied by the message's current state."""
    add = [l for l in actions.add_label if l not in meta.labels]
    remove = [l for l in actions.remove_label if l in meta.labels]
    archive = actions.archive and "INBOX" in meta.labels
    changed = bool(add or remove or archive)
    return Actions(add_label=add, remove_label=remove, archive=archive), changed


def scan(client: GmailClient, config: Config, limit: int | None = None) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen: set[str] = set()
    # One label index for the whole scan; scan never creates labels.
    index = client.fetch_label_index()
    for rule in config.rules:
        ids = client.list(query_from_match(rule.match), limit=limit)
        for msg_id in ids:
            if msg_id in seen:
                continue
            seen.add(msg_id)
            meta = client.get_meta(msg_id, index)
            matched = first_matching_rule(config, meta)
            if matched is None or matched.id != rule.id:
                continue  # another rule won, or message excluded/not included
            actions, changed = noop_eliminate(meta, rule.actions)
            if not changed:
                continue
            candidates.append(Candidate(
                message_id=meta.id,
                thread_id=meta.thread_id,
                rule_id=rule.id,
                actions=actions,
                before_labels=set(meta.labels),
                in_inbox="INBOX" in meta.labels,
            ))
    return candidates


def apply_run(client: GmailClient, config: Config, candidates: list[Candidate],
              journal: RunJournal, audit: AuditLog, run_id: str,
              confirm: Callable[[], bool]) -> int:
    """Re-verify every candidate against current state, then write. Returns exit code."""
    if not confirm():
        return EXIT_CANCELLED
    # One label index for the whole run. Add labels are created here (the only
    # write path that may create labels); remove labels that do not exist on
    # the account are skipped.
    index = client.fetch_label_index()
    failed = 0
    for cand in candidates:
        try:
            meta = client.get_meta(cand.message_id, index)
        except Exception:
            journal.record_failure(run_id, cand.message_id, "message gone or unreadable")
            failed += 1
            continue
        if is_excluded(config, meta):
            continue
        fresh, changed = noop_eliminate(meta, cand.actions)
        if not changed:
            continue
        # Write boundary: resolve canonical names to Gmail label IDs.
        add_ids: list[str] = []
        for name in fresh.add_label:
            label_id = client.ensure_label(name, index)  # create if missing
            add_ids.append(label_id)
        remove_ids: list[str] = []
        for name in fresh.remove_label:
            label_id = index.name_to_id(name)
            if label_id is None:
                continue  # label does not exist on the account; nothing to remove
            remove_ids.append(label_id)
        if fresh.archive:
            inbox_id = index.name_to_id("INBOX")
            if inbox_id is not None:
                remove_ids.append(inbox_id)
        if not add_ids and not remove_ids:
            continue
        try:
            client.batch_modify([meta.id], add=add_ids, remove=remove_ids)
        except Exception as exc:
            journal.record_failure(run_id, cand.message_id, str(exc))
            failed += 1
            continue
        for label in fresh.add_label:
            audit.append(AuditEntry(run_id=run_id, message_id=meta.id, thread_id=meta.thread_id,
                                    rule_id=cand.rule_id, action="add_label", payload=label))
        for label in fresh.remove_label:
            audit.append(AuditEntry(run_id=run_id, message_id=meta.id, thread_id=meta.thread_id,
                                    rule_id=cand.rule_id, action="remove_label", payload=label))
        if fresh.archive:
            audit.append(AuditEntry(run_id=run_id, message_id=meta.id, thread_id=meta.thread_id,
                                    rule_id=cand.rule_id, action="archive", payload="INBOX"))
    return EXIT_PARTIAL if failed else EXIT_OK
