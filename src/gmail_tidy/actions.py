"""Planning (scan) and the only write path (apply). Reconcile-before-apply."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gmail_tidy.audit import AuditEntry, AuditLog, Candidate, RunJournal
from gmail_tidy.checkpoint import RuleCheckpoint, ScanCheckpoint, config_fingerprint
from gmail_tidy.config import Actions, Config, MatchConfig
from gmail_tidy.errors import AuthError, EXIT_CANCELLED, EXIT_OK, EXIT_PARTIAL
from gmail_tidy.gmail_client import GmailClient
from gmail_tidy.rules import (
    MessageMeta,
    _SPECIAL_CATEGORIES,
    first_matching_rule,
    is_excluded,
    is_included,
)


def query_from_match(match: MatchConfig) -> str:
    """Best-effort Gmail search narrowing (bare terms). Never the source of truth.

    Eligibility is always re-decided locally by first_matching_rule against
    fetched metadata; this query only narrows what gets fetched. Bare terms
    (no operator syntax) are used deliberately so narrowing degrades safely
    to "fetch more, filter locally" rather than depending on exact operator
    support from whatever is on the other end of GmailClient.list().

    Text-probe categories narrow with their bare category term. The special
    categories (_SPECIAL_CATEGORIES) have no meaningful Gmail search term and
    contribute nothing: a bare "old_unread"/"large_messages" word would never
    appear in From/Subject and would starve the scan, so those rules fetch
    everything and filter locally in rules.matches_rule.
    """
    parts: list[str] = []
    parts.extend(match.subject_contains)
    parts.extend(match.from_contains)
    if match.category and match.category not in _SPECIAL_CATEGORIES:
        parts.append(match.category)
    return " ".join(parts)


def noop_eliminate(meta: MessageMeta, actions: Actions) -> tuple[Actions, bool]:
    """Drop actions already satisfied by the message's current state."""
    add = [l for l in actions.add_label if l not in meta.labels]
    remove = [l for l in actions.remove_label if l in meta.labels]
    archive = actions.archive and "INBOX" in meta.labels
    changed = bool(add or remove or archive)
    return Actions(add_label=add, remove_label=remove, archive=archive), changed


@dataclass
class ScanStats:
    evaluated: int = 0    # passed the rule-match check (eligible for this rule)
    excluded: int = 0     # matched is None or another rule won (excluded/protected/not-included/other-rule)
    noop: int = 0         # matched this rule but noop_eliminate found nothing to change
    candidates: int = 0   # appended as a Candidate


def scan(client: GmailClient, config: Config, limit: int | None = None,
         checkpoint: ScanCheckpoint | None = None, full: bool = False,
         on_progress: Callable[[ScanCheckpoint, str, int], None] | None = None
         ) -> tuple[list[Candidate], ScanCheckpoint, ScanStats]:
    """Build the candidate plan, paginating rule-by-rule with resumable state.

    ``limit`` is a GLOBAL target for total NEW eligible candidates across all
    rules — not a per-rule fetch cap and not a raw number of messages fetched.
    Already-labeled/no-op/excluded messages do not count toward the limit.

    ``full`` (the --all path) scans to exhaustion: it paginates each rule until
    its query returns no next page, marks that rule exhausted=True, and on later
    --all runs skips already-exhausted rules entirely (zero fetches). ``full``
    and ``limit`` are mutually exclusive.

    ``on_progress``, when given, is called synchronously right after a rule's
    pagination reaches exhaustion (next_tok is None), for both full and non-full
    scans, with (checkpoint_so_far, rule_id, candidates_so_far).

    Pagination resumes from ``checkpoint.rules[rule.id].page_token`` if present,
    so a repeat scan makes forward progress through the mailbox instead of
    re-fetching page 1. The returned ScanCheckpoint records, for every rule, the
    resume point to continue from on the next invocation. When the limit is hit,
    the CURRENT page's token (the one that produced the limit-hitting candidate)
    is persisted so the next scan re-fetches that same page from the start rather
    than skipping as-yet-unevaluated messages in it.

    Scan remains fully read-only: it never calls GmailClient.list() here, only
    list_page() (a single-page fetch), and never modifies any message.
    """
    if full and limit is not None:
        raise ValueError("full=True and limit are mutually exclusive")
    candidates: list[Candidate] = []
    seen: set[str] = set()
    # Prefer the loaded checkpoint's fingerprint when one was passed: the CLI
    # and runner load checkpoints with the FULL config, so its fingerprint is
    # the full-config hash even when `config` was filtered down to a --rules
    # subset for this scan pass. Keeping it stable (identical to the unfiltered
    # hash) preserves the "editing config.yaml invalidates the checkpoint"
    # safety property for both scoped and unscoped scans.
    fp = checkpoint.config_fingerprint if checkpoint is not None else config_fingerprint(config)
    new_cp = ScanCheckpoint(config_fingerprint=fp)
    stats = ScanStats()
    # One label index for the whole scan; scan never creates labels.
    index = client.fetch_label_index()
    for rule in config.rules:
        if (full and checkpoint is not None and rule.id in checkpoint.rules
                and checkpoint.rules[rule.id].exhausted):
            # Already fully consumed by a prior --all run: skip entirely (ZERO
            # list_page calls) and carry the prior exhausted entry forward.
            new_cp.rules[rule.id] = RuleCheckpoint(
                page_token=checkpoint.rules[rule.id].page_token,
                exhausted=True,
            )
            continue
        tok = None
        if checkpoint is not None and rule.id in checkpoint.rules:
            tok = checkpoint.rules[rule.id].page_token
        while True:
            page_ids, next_tok = client.list_page(query_from_match(rule.match), page_token=tok)
            for msg_id in page_ids:
                if msg_id in seen:
                    continue
                meta = client.get_meta(msg_id, index)
                matched = first_matching_rule(config, meta)
                if matched is None or matched.id != rule.id:
                    # another rule won, or message excluded/not included/protected
                    stats.excluded += 1
                    continue
                # Dedup only after THIS rule claims the message: a message that
                # an earlier rule's query fetched but that a later rule matches
                # (first-match-wins) must stay claimable by the later rule.
                seen.add(msg_id)
                stats.evaluated += 1
                actions, changed = noop_eliminate(meta, rule.actions)
                if not changed:
                    stats.noop += 1
                    continue
                stats.candidates += 1
                candidates.append(Candidate(
                    message_id=meta.id,
                    thread_id=meta.thread_id,
                    rule_id=rule.id,
                    actions=actions,
                    before_labels=set(meta.labels),
                    in_inbox="INBOX" in meta.labels,
                ))
                if limit is not None and len(candidates) >= limit:
                    # Persist the CURRENT page's token (the one that produced the
                    # limit-hitting candidate), not next_tok, so the next scan
                    # re-evaluates this same page from the start.
                    new_cp.rules[rule.id] = RuleCheckpoint(page_token=tok)
                    return candidates, new_cp, stats
            if next_tok is None:
                # Mailbox exhausted for this rule/query; record a clean resume
                # point (page_token=None). exhausted mirrors `full`: only --all
                # may claim a rule is truly done (so a later --all re-scans a
                # rule a plain/--limit scan merely reached the end of).
                new_cp.rules[rule.id] = RuleCheckpoint(page_token=None, exhausted=full)
                if on_progress is not None:
                    on_progress(new_cp, rule.id, len(candidates))
                break
            tok = next_tok
    return candidates, new_cp, stats


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
        except AuthError:
            # A mid-run 403 (revoked/expired token) must propagate to the
            # caller's auth exit path — not be swallowed as a per-message
            # failure that would report a misleading "partial success".
            raise
        except Exception:
            journal.record_failure(run_id, cand.message_id, "message gone or unreadable")
            failed += 1
            continue
        if is_excluded(config, meta) or not is_included(config, meta):
            # Re-verify the include guard at write time exactly as scan did
            # (first_matching_rule requires is_included AND not is_excluded).
            # A candidate whose include label/text match disappeared between
            # scan and apply is skipped with no mailbox change and no audit
            # entry. An empty include list remains allowed (include all).
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
        except AuthError:
            # Same as the get_meta block above: a 403 is an auth failure, not a
            # per-message partial failure — propagate for the auth exit path.
            raise
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
