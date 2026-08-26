# tests/test_actions.py
from datetime import datetime, timezone
from pathlib import Path
import pytest
from gmail_tidy.config import Config, Rule, MatchConfig, Actions
from gmail_tidy.actions import scan, apply_run, ScanStats, query_from_match
from gmail_tidy.audit import RunJournal, AuditLog, Candidate
from gmail_tidy.checkpoint import (
    RuleCheckpoint,
    ScanCheckpoint,
    checkpoint_path,
    config_fingerprint,
    load_checkpoint,
    merge_checkpoint,
    save_checkpoint,
)
from gmail_tidy.gmail_client import GmailClient
from gmail_tidy.errors import EXIT_OK, EXIT_CANCELLED, EXIT_PARTIAL, AuthError
from tests.mock_gmail import MockGmailApi, _GError


def _config():
    return Config(
        rules=[
            Rule(id="r1", match=MatchConfig(subject_contains=["newsletter"], older_than_days=10),
                 actions=Actions(add_label=["Cleanup/N"], archive=True)),
        ]
    )


def test_scan_builds_candidates():
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    api.add_message("m2", subject="receipt", labels={"INBOX"})
    c, _cp, _stats = scan(GmailClient(api), _config())
    assert [x.message_id for x in c] == ["m1"]
    assert c[0].actions.add_label == ["Cleanup/N"]
    assert c[0].in_inbox is True
    assert c[0].before_labels == {"INBOX"}


def test_apply_skips_newly_excluded_message(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", subject="news", labels={"IMPORTANT"})  # protected now
    client = GmailClient(api)
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(add_label=["Cleanup/N"], archive=True),
                     before_labels={"INBOX"}, in_inbox=True)
    j.save_candidates(run_id, [cand])
    apply_run(client, _config(), [cand], j, audit, run_id, confirm=lambda: True)
    assert "Cleanup/N" not in api.label_names_of("m1")
    assert not audit.entries()


def _include_config():
    """Config with a label-based include guard: only messages still carrying
    the 'Keep' label are eligible for r1."""
    return Config(
        include=["label:Keep"],
        rules=[
            Rule(id="r1", match=MatchConfig(subject_contains=["newsletter"]),
                 actions=Actions(add_label=["Cleanup/N"], archive=True)),
        ]
    )


def test_apply_skips_candidate_that_lost_its_include_label(tmp_path):
    """A candidate that was eligible at scan time but lost its include label
    before apply must be skipped: no mailbox change and no audit entry."""
    api = MockGmailApi()
    # at scan time the message carries the include label 'Keep' -> candidate
    api.add_message("m1", subject="newsletter", labels={"INBOX", "Keep"})
    cfg = _include_config()
    client = GmailClient(api)
    cands, _cp, _stats = scan(client, cfg)
    assert [c.message_id for c in cands] == ["m1"]
    # the include label vanishes between scan and apply (e.g. another process
    # re-labelled the message, or a Gmail filter did)
    api.store["m1"].label_ids.discard(api.label_id("Keep"))
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    j.save_candidates(run_id, cands)
    result = apply_run(client, cfg, cands, j, audit, run_id, confirm=lambda: True)
    assert result == EXIT_OK
    # no mailbox change and no audit entry for the now-unincluded message
    assert api.label_names_of("m1") == {"INBOX"}
    assert not audit.entries()
    assert j.failures(run_id) == []


def test_apply_empty_include_remains_allowed(tmp_path):
    """An empty include list means 'include everything'; apply must not skip
    an otherwise-eligible candidate just because include is empty."""
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    client = GmailClient(api)
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(add_label=["Cleanup/N"], archive=True),
                     before_labels={"INBOX"}, in_inbox=True)
    j.save_candidates(run_id, [cand])
    result = apply_run(client, _config(), [cand], j, audit, run_id, confirm=lambda: True)
    assert result == EXIT_OK
    assert "Cleanup/N" in api.label_names_of("m1")
    assert len(audit.entries()) == 2  # add_label + archive


def test_apply_audits_each_action(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", subject="news", labels={"INBOX"})
    client = GmailClient(api)
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(add_label=["Cleanup/N"], archive=True),
                     before_labels={"INBOX"}, in_inbox=True)
    j.save_candidates(run_id, [cand])
    result = apply_run(client, _config(), [cand], j, audit, run_id, confirm=lambda: True)
    assert result == EXIT_OK
    assert len(audit.entries()) == 2  # add_label + archive
    assert "Cleanup/N" in api.label_names_of("m1")
    assert "INBOX" not in api.label_names_of("m1")


def test_apply_cancel_is_exit_5(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", subject="news", labels={"INBOX"})
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(archive=True), before_labels={"INBOX"}, in_inbox=True)
    result = apply_run(GmailClient(api), _config(), [cand], j, audit, run_id, confirm=lambda: False)
    assert result == EXIT_CANCELLED
    assert not audit.entries()


# --- label boundary behavior ---------------------------------------------


def test_scan_never_creates_labels():
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    scan(GmailClient(api), _config())  # returns (candidates, checkpoint, stats)
    # the add_label target must NOT exist after a scan
    assert not api.has_label("Cleanup/N")
    assert "Cleanup/N" not in api.label_names_of("m1")


def test_apply_creates_missing_add_label(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", subject="news", labels={"INBOX"})
    client = GmailClient(api)
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(add_label=["Cleanup/N"], archive=True),
                     before_labels={"INBOX"}, in_inbox=True)
    j.save_candidates(run_id, [cand])
    result = apply_run(client, _config(), [cand], j, audit, run_id, confirm=lambda: True)
    assert result == EXIT_OK
    assert "Cleanup/N" in api.label_names_of("m1")
    # the label was created on the account
    assert api.has_label("Cleanup/N")


def test_apply_resolves_existing_add_label(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", subject="news", labels={"INBOX"})
    api.add_message("seed", labels={"Cleanup/N"})  # label already exists
    client = GmailClient(api)
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(add_label=["Cleanup/N"], archive=True),
                     before_labels={"INBOX"}, in_inbox=True)
    j.save_candidates(run_id, [cand])
    result = apply_run(client, _config(), [cand], j, audit, run_id, confirm=lambda: True)
    assert result == EXIT_OK
    assert "Cleanup/N" in api.label_names_of("m1")


def test_apply_skips_unresolved_remove_label(tmp_path):
    """A remove_label naming a label that does not exist on the account is
    skipped (nothing to remove) rather than failing the run."""
    api = MockGmailApi()
    api.add_message("m1", subject="news", labels={"INBOX"})
    client = GmailClient(api)
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(remove_label=["Ghost/Nonexistent"]),
                     before_labels={"INBOX"}, in_inbox=True)
    j.save_candidates(run_id, [cand])
    result = apply_run(client, _config(), [cand], j, audit, run_id, confirm=lambda: True)
    assert result == EXIT_OK
    assert not audit.entries()
    assert api.label_names_of("m1") == {"INBOX"}


def test_apply_skips_remove_label_that_cannot_resolve(tmp_path, monkeypatch):
    """Defensive guard: if a remove label's name cannot be resolved to an ID
    at write time, it is skipped rather than sent to batchModify."""
    api = MockGmailApi()
    api.add_message("m1", subject="news", labels={"INBOX", "Cleanup/N"})
    client = GmailClient(api)
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(remove_label=["Cleanup/N"]),
                     before_labels={"INBOX", "Cleanup/N"}, in_inbox=True)
    j.save_candidates(run_id, [cand])
    # force the resolution to fail for this name
    orig = client.fetch_label_index
    client.fetch_label_index = lambda: _IndexWithout("Cleanup/N")
    try:
        result = apply_run(client, _config(), [cand], j, audit, run_id, confirm=lambda: True)
    finally:
        client.fetch_label_index = orig
    assert result == EXIT_OK
    assert "Cleanup/N" in api.label_names_of("m1")  # untouched
    assert not audit.entries()


class _IndexWithout:
    """LabelIndex stand-in that resolves IDs on read but cannot resolve a
    specific name on write (simulates a label vanishing mid-run)."""

    def __init__(self, missing: str):
        self._missing = missing
        self._id_to_name = {"INBOX": "INBOX", "Label_1": "Cleanup/N"}

    def name_to_id(self, name: str) -> str | None:
        return None if name == self._missing else name

    def id_to_name(self, label_id: str) -> str | None:
        return self._id_to_name.get(label_id, label_id)


def test_apply_audits_names_not_ids(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", subject="news", labels={"INBOX"})
    client = GmailClient(api)
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(add_label=["Cleanup/N"], archive=True),
                     before_labels={"INBOX"}, in_inbox=True)
    j.save_candidates(run_id, [cand])
    apply_run(client, _config(), [cand], j, audit, run_id, confirm=lambda: True)
    payloads = [e.payload for e in audit.entries()]
    assert "Cleanup/N" in payloads
    assert "INBOX" in payloads
    assert api.label_id("Cleanup/N") not in payloads


# --- scan pagination / limit semantics (approved fix) --------------------


def _multi_rule_config():
    """Two rules, each narrowing on a distinct subject so candidates are
    separated by rule. Order matters: r1 is tried first."""
    return Config(
        rules=[
            Rule(id="r1", match=MatchConfig(subject_contains=["alpha"]),
                 actions=Actions(add_label=["Cleanup/A"], archive=True)),
            Rule(id="r2", match=MatchConfig(subject_contains=["beta"]),
                 actions=Actions(add_label=["Cleanup/B"], archive=True)),
        ]
    )


def test_scan_limit_is_global_across_rules():
    """limit is a GLOBAL target across all rules, not per-rule."""
    api = MockGmailApi()
    for i in range(4):
        api.add_message(f"a{i}", subject="alpha", labels={"INBOX"})
    for i in range(4):
        api.add_message(f"b{i}", subject="beta", labels={"INBOX"})
    cfg = _multi_rule_config()
    cands, cp, _stats = scan(GmailClient(api), cfg, limit=3)
    assert len(cands) == 3
    # all three candidates are eligible (not no-op), gathered across rules
    assert {c.rule_id for c in cands} <= {"r1", "r2"}
    # the limit was hit, so a resume point was persisted
    assert cp.config_fingerprint == config_fingerprint(cfg)
    assert cp.rules


def test_scan_paginates_past_noop_messages_within_one_call():
    """Pagination must continue past already-processed/no-op page-1 messages
    and reach new eligible candidates on page 2, all in a single scan call."""
    api = MockGmailApi()
    # page 1 (2 messages): already labeled + archived -> no-op
    api.add_message("m1", subject="newsletter", labels={"Cleanup/N"})
    api.add_message("m2", subject="newsletter", labels={"Cleanup/N"})
    # page 2: fresh, still eligible
    api.add_message("m3", subject="newsletter", labels={"INBOX"})
    api.add_message("m4", subject="newsletter", labels={"INBOX"})
    cands, _cp, _stats = scan(GmailClient(api), _config())
    assert [c.message_id for c in cands] == ["m3", "m4"]


def test_scan_checkpoint_resumes_past_consumed_pages():
    """(c) A scan that hits --limit mid-mailbox persists a page_token. Once
    those candidates are applied (become no-op), a SECOND CLI-style invocation
    seeded with the persisted checkpoint continues past the consumed pages and
    finds the still-eligible messages beyond them, instead of re-fetching page 1
    and returning empty."""
    cfg = _config()
    api = MockGmailApi()
    for i in range(1, 7):
        api.add_message(f"m{i}", subject="newsletter", labels={"INBOX"})
    client = GmailClient(api)

    # invoke 1: fresh scan, limit=4. Page size is 2, so it gathers m1..m4
    # (pages 1-2) and stops mid-mailbox, persisting the page-2 resume token.
    cands1, cp1, _stats1 = scan(client, cfg, limit=4)
    assert [c.message_id for c in cands1] == ["m1", "m2", "m3", "m4"]
    assert cp1.rules["r1"].page_token is not None  # resume point persisted

    # apply: mark the four candidates as already handled (label present, no
    # longer in INBOX) so they are no-op on the next scan.
    for c in cands1:
        api.store[c.message_id].label_ids = {
            api.label_id(l) for l in c.before_labels | set(c.actions.add_label)
        } - {"INBOX"}

    # invoke 2: fresh CLI-style invocation resumed from the persisted checkpoint
    cands2, cp2, _stats2 = scan(client, cfg, limit=10, checkpoint=cp1)
    # found the beyond-page-2 messages that the first invocation never reached
    assert [c.message_id for c in cands2] == ["m5", "m6"]
    # mailbox fully consumed -> clean resume point
    assert cp2.rules["r1"].page_token is None


def test_scan_invalidated_checkpoint_restarts_from_page_1(tmp_path):
    """(d) a stored checkpoint with a stale page_token is ignored when the
    config changes (different fingerprint), so scanning restarts from page 1."""
    cfg1 = _config()
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    api.add_message("m2", subject="newsletter", labels={"INBOX"})
    api.add_message("m3", subject="newsletter", labels={"INBOX"})
    client = GmailClient(api)

    # config changes -> new fingerprint, so a stored checkpoint for cfg1 with a
    # stale page_token is ignored and scanning restarts from page 1.
    cfg2 = Config(
        exclude=[MatchConfig(from_contains=["nonexistent"])],
        rules=cfg1.rules,
    )
    assert config_fingerprint(cfg2) != config_fingerprint(cfg1)
    # load via the checkpoint module to emulate cli behaviour; cfg1's checkpoint
    # carries a different fingerprint so it is dropped on load for cfg2.
    stale = ScanCheckpoint(
        config_fingerprint=config_fingerprint(cfg1),
        rules={"r1": RuleCheckpoint(page_token="99")},
    )
    p = checkpoint_path(tmp_path)
    save_checkpoint(p, stale)
    loaded = load_checkpoint(p, cfg2)
    assert loaded.rules == {}  # invalidated -> no resume token
    cands, _cp, _stats = scan(client, cfg2, checkpoint=loaded)
    # restarts from page 1 and finds the page-1 candidate (m1) that a resume at
    # the stale token ("99", i.e. past everything) would have skipped.
    assert [c.message_id for c in cands] == ["m1", "m2", "m3"]


# --- scan --all / full exhaustion -----------------------------------------


def _spy_list_page(client):
    """Wrap client.list_page to record every (query, page_token) invocation.

    Returns (wrapped_client, queries_seen). queries_seen maps query string ->
    list of page_tokens it was called with, so a test can assert a rule's query
    was never fetched.
    """
    seen: dict[str, list] = {}
    orig = client.list_page

    def spy(query="", page_token=None):
        seen.setdefault(query, []).append(page_token)
        return orig(query, page_token)

    client.list_page = spy
    return client, seen


def test_scan_all_paginates_to_exhaustion():
    """full=True walks every page (3+ pages at page_size=2) and marks the rule
    exhausted=True with a clean (None) resume point."""
    api = MockGmailApi()
    for i in range(6):  # 3 pages
        api.add_message(f"m{i}", subject="newsletter", labels={"INBOX"})
    cands, cp, _stats = scan(GmailClient(api), _config(), full=True)
    assert [c.message_id for c in cands] == [f"m{i}" for i in range(6)]
    assert cp.rules["r1"].exhausted is True
    assert cp.rules["r1"].page_token is None


def test_scan_all_skips_already_processed_first_page():
    """Page 1 already-processed (no-op) messages must not block full exhaustion
    from reaching fresh candidates on later pages."""
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"Cleanup/N"})  # no-op
    api.add_message("m2", subject="newsletter", labels={"Cleanup/N"})  # no-op
    api.add_message("m3", subject="newsletter", labels={"INBOX"})
    api.add_message("m4", subject="newsletter", labels={"INBOX"})
    cands, cp, _stats = scan(GmailClient(api), _config(), full=True)
    assert [c.message_id for c in cands] == ["m3", "m4"]
    assert cp.rules["r1"].exhausted is True


def test_scan_all_resumes_skipping_exhausted_rule():
    """A checkpoint with rule A exhausted=True must skip A entirely — ZERO
    list_page calls for A's query — while rule B (not exhausted, mid-pagination
    token) is still resumed from its page_token."""
    api = MockGmailApi()
    for i in range(4):
        api.add_message(f"a{i}", subject="alpha", labels={"INBOX"})
    for i in range(4):
        api.add_message(f"b{i}", subject="beta", labels={"INBOX"})
    cfg = _multi_rule_config()
    cp = ScanCheckpoint(
        config_fingerprint=config_fingerprint(cfg),
        rules={
            "r1": RuleCheckpoint(page_token=None, exhausted=True),
            "r2": RuleCheckpoint(page_token="2", exhausted=False),  # resume mid-mailbox
        },
    )
    client, seen = _spy_list_page(GmailClient(api))
    cands, new_cp, _stats = scan(client, cfg, full=True, checkpoint=cp)
    # r1 exhausted -> never fetched; r2 resumes from token "2" (b2, b3 eligible)
    assert "alpha" not in seen
    assert seen.get("beta") == ["2"]  # resumed at "2", exhausted in one more page
    assert [c.message_id for c in cands] == ["b2", "b3"]
    assert new_cp.rules["r1"].exhausted is True
    assert new_cp.rules["r2"].exhausted is True
    assert new_cp.rules["r2"].page_token is None


def test_scan_all_exhausted_rule_skipped_on_third_call():
    """Once a rule is exhausted, every later --all call skips it (no fetch),
    even with new mail arriving — only a fresh non-exhausted rule is scanned."""
    api = MockGmailApi()
    for i in range(4):
        api.add_message(f"m{i}", subject="newsletter", labels={"INBOX"})
    cfg = _config()

    # call 1: --all exhausts r1, marks it exhausted
    c1, cp1, _ = scan(GmailClient(api), cfg, full=True)
    assert len(c1) == 4
    assert cp1.rules["r1"].exhausted is True

    # calls 2 and 3: skip r1 entirely (already exhausted) -> no candidates, no fetch
    for cp in (cp1, cp1):
        client, seen = _spy_list_page(GmailClient(api))
        cands, new_cp, _ = scan(client, cfg, full=True, checkpoint=cp)
        assert cands == []
        assert seen == {}  # zero list_page calls
        assert new_cp.rules["r1"].exhausted is True


def test_scan_all_empty_mailbox_marks_exhausted():
    api = MockGmailApi()
    cands, cp, stats = scan(GmailClient(api), _config(), full=True)
    assert cands == []
    assert cp.rules["r1"].exhausted is True
    assert cp.rules["r1"].page_token is None


def test_scan_all_config_fingerprint_invalidation(tmp_path):
    """A stored checkpoint for a different config (different fingerprint) is
    dropped even when it carries exhausted=True — --all restarts from page 1."""
    cfg1 = _config()
    api = MockGmailApi()
    for i in range(4):
        api.add_message(f"m{i}", subject="newsletter", labels={"INBOX"})
    cfg2 = Config(
        exclude=[MatchConfig(from_contains=["nonexistent"])],
        rules=cfg1.rules,
    )
    assert config_fingerprint(cfg2) != config_fingerprint(cfg1)
    stale = ScanCheckpoint(
        config_fingerprint=config_fingerprint(cfg1),
        rules={"r1": RuleCheckpoint(page_token="99", exhausted=True)},
    )
    p = checkpoint_path(tmp_path)
    save_checkpoint(p, stale)
    loaded = load_checkpoint(p, cfg2)
    assert loaded.rules == {}  # invalidated -> no resume, not skipped
    cands, new_cp, _ = scan(GmailClient(api), cfg2, full=True, checkpoint=loaded)
    assert [c.message_id for c in cands] == [f"m{i}" for i in range(4)]
    assert new_cp.rules["r1"].exhausted is True


def test_scan_full_and_limit_mutually_exclusive():
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    with pytest.raises(ValueError):
        scan(GmailClient(api), _config(), full=True, limit=5)


def test_scan_stats_counters():
    """excluded / noop / candidate split, plus the evaluated==noop+candidates
    invariant, all tracked through ScanStats."""
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"IMPORTANT"})  # protected -> excluded
    api.add_message("m2", subject="newsletter", labels={"Cleanup/N"})  # already done -> noop
    api.add_message("m3", subject="newsletter", labels={"INBOX"})      # fresh -> candidate
    api.add_message("m4", subject="newsletter", labels={"INBOX"})      # fresh -> candidate
    cands, _cp, stats = scan(GmailClient(api), _config(), full=True)
    assert [c.message_id for c in cands] == ["m3", "m4"]
    assert stats.excluded == 1
    assert stats.evaluated == 3
    assert stats.noop == 1
    assert stats.candidates == 2
    assert stats.evaluated == stats.noop + stats.candidates


def test_scan_all_does_not_regress_limit_semantics():
    """A plain --limit scan is unaffected by the --all change: it still returns
    all eligible candidates and records exhausted=False even when it happens to
    fully exhaust a rule's mailbox (so a later --all re-scans it)."""
    api = MockGmailApi()
    for i in range(4):
        api.add_message(f"a{i}", subject="alpha", labels={"INBOX"})
    for i in range(4):
        api.add_message(f"b{i}", subject="beta", labels={"INBOX"})
    cfg = _multi_rule_config()
    # limit=10 exceeds the total eligible count (8), so neither rule's query is
    # cut off by the limit early-return: pagination runs both rules to real
    # exhaustion (next_tok is None) with full=False, which is exactly the branch
    # (actions.py line 138, exhausted=full) this test must exercise.
    cands, cp, _stats = scan(GmailClient(api), cfg, limit=10)
    assert len(cands) == 8
    assert {c.rule_id for c in cands} <= {"r1", "r2"}
    # a --limit scan must NEVER claim exhaustion — even though it happens to sit
    # at the end of a rule's mailbox, only a --all (full=True) scan may record
    # exhausted=True, so a later --all run re-scans that rule for new mail.
    assert cp.rules and all(r.exhausted is False for r in cp.rules.values())


# --- scoped (--rules) scan preserves unselected rules' checkpoint state -----


def test_scan_scoped_merges_unselected_rules_prior_entries():
    """A scoped scan of a filtered config with a checkpoint loaded from the FULL
    config's fingerprint must carry the unselected rule's prior entry forward
    (so a later save keeps every previously-scanned rule), keep the full-config
    fingerprint, and still resume the selected rule from its own page_token."""
    cfg = _multi_rule_config()
    api = MockGmailApi()
    # r2's mailbox: one already-consumed page (m1), one fresh eligible page (m2)
    api.add_message("m1", subject="beta", labels={"INBOX"})
    api.add_message("m2", subject="beta", labels={"INBOX"})
    # r1's mailbox: a fresh candidate beyond page 1
    api.add_message("a1", subject="alpha", labels={"INBOX"})
    api.add_message("a2", subject="alpha", labels={"INBOX"})

    # A prior checkpoint loaded with the FULL config fingerprint, r2 already
    # mid-pagination (page 1 consumed -> page_token "1").
    full_fp = config_fingerprint(cfg)
    prior = ScanCheckpoint(
        config_fingerprint=full_fp,
        rules={"r2": RuleCheckpoint(page_token="1", exhausted=False)},
    )
    client, seen = _spy_list_page(GmailClient(api))

    # The CLI filters cfg to the selected rules for the scan pass but keeps the
    # full-config fingerprint in the checkpoint object it passes in.
    filtered = Config(rules=[cfg.rules[0]])  # r1 only
    cands, new_cp, _stats = scan(client, filtered, checkpoint=prior)
    # resumes r1 from page 1 (no prior r1 entry) and finds a1, a2
    assert {c.message_id for c in cands} == {"a1", "a2"}
    assert seen["alpha"] == [None]
    # scan itself only records the rules it touched; the returned checkpoint
    # keeps the FULL-config fingerprint (not the filtered config's hash)
    assert new_cp.config_fingerprint == full_fp
    assert "r2" not in new_cp.rules  # unselected rule untouched by this pass
    assert new_cp.rules["r1"].exhausted is False

    # Save-time merge (done by the CLI/runner before save): the unselected
    # rule's prior entry is carried forward and the full-config hash is kept.
    merged = merge_checkpoint(prior, new_cp)
    assert merged.rules["r2"].page_token == "1"
    assert merged.rules["r2"].exhausted is False
    assert merged.rules["r1"] == new_cp.rules["r1"]  # selected rule kept fresh
    assert merged.config_fingerprint == full_fp


# --- mid-run AuthError propagates (Task 30) --------------------------------
# A 403 (revoked/expired token) mid-apply must propagate out of apply_run as
# AuthError — NOT be recorded as a per-message failure that reports a
# misleading "partial success" — so the CLI's auth exit path (exit 4) fires.


def _apply_harness(tmp_path):
    """Standard apply_run harness: journal + audit + fresh run_id."""
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    return j, audit, run_id


def _cand(mid: str) -> Candidate:
    return Candidate(message_id=mid, thread_id=f"t-{mid}", rule_id="r1",
                     actions=Actions(add_label=["Cleanup/N"], archive=True),
                     before_labels={"INBOX"}, in_inbox=True)


def _inject_get(api, fail_at_call: int, exc):
    """Wrap the mock's get handler to raise ``exc`` on the n-th get call."""
    calls = {"n": 0}
    orig = api._handlers["get"]

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == fail_at_call:
            raise exc
        return orig(**kw)

    api._handlers["get"] = flaky


def test_apply_get_meta_403_propagates_and_not_recorded(tmp_path):
    """A 403 on get_meta mid-run (after m1 already applied) must escape
    apply_run as AuthError — never a recorded per-message failure."""
    api = MockGmailApi()
    api.add_message("m1", subject="news", labels={"INBOX"})
    api.add_message("m2", subject="news", labels={"INBOX"})
    client = GmailClient(api)
    j, audit, run_id = _apply_harness(tmp_path)
    # apply_run fetches labels (labels.list) then get_meta per candidate:
    # m1's get_meta (call 1) succeeds, m2's get_meta (call 2) hits the 403.
    _inject_get(api, fail_at_call=2, exc=_GError(403, "denied"))
    with pytest.raises(AuthError):
        apply_run(client, _config(), [_cand("m1"), _cand("m2")], j, audit,
                  run_id, confirm=lambda: True)
    # m1 WAS applied; m2's 403 was NOT recorded as a per-message failure.
    assert "Cleanup/N" in api.label_names_of("m1")
    assert j.failures(run_id) == []


def test_403_batch_modify_propagates_and_not_recorded(tmp_path):
    """A 403 on batch_modify mid-run must raise AuthError out of apply_run,
    not be swallowed into a partial-failure report."""
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    api.add_message("m2", subject="newsletter", labels={"INBOX"})
    client = GmailClient(api)
    j, audit, run_id = _apply_harness(tmp_path)
    calls = {"n": 0}
    orig = api._handlers["batchModify"]

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 2:  # m2's write fails with 403 after m1 succeeded
            raise _GError(403, "denied")
        return orig(**kw)

    api._handlers["batchModify"] = flaky
    with pytest.raises(AuthError):
        apply_run(client, _config(), [_cand("m1"), _cand("m2")], j, audit,
                  run_id, confirm=lambda: True)
    # m1 was applied; the 403 was not counted as a failure.
    assert "Cleanup/N" in api.label_names_of("m1")
    assert j.failures(run_id) == []


def test_persistent_500_still_recorded_as_failure(tmp_path, monkeypatch):
    """Regression guard: a persistent RequestError (500) is STILL swallowed
    and recorded as a per-message failure — the AuthError re-raise must not
    broaden to other exception types. Run completes with EXIT_PARTIAL."""
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    api.add_message("m2", subject="newsletter", labels={"INBOX"})
    client = GmailClient(api)
    j, audit, run_id = _apply_harness(tmp_path)
    # batchModify fails 500 on every call (retries exhausted -> RequestError)
    api._handlers["batchModify"] = lambda **kw: (_ for _ in ()).throw(_GError(500, "boom"))
    monkeypatch.setattr("time.sleep", lambda s: None)
    result = apply_run(client, _config(), [_cand("m1"), _cand("m2")], j, audit,
                       run_id, confirm=lambda: True)
    assert result == EXIT_PARTIAL
    # both batch writes failed and were recorded per-message
    assert "Cleanup/N" not in api.label_names_of("m1")
    assert "Cleanup/N" not in api.label_names_of("m2")
    assert len(j.failures(run_id)) == 2


def test_generic_exception_still_recorded_and_loop_continues(tmp_path):
    """Regression guard: a non-auth failure (message gone/unreadable) is STILL
    recorded as a failure and the loop continues — only AuthError changes."""
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    api.add_message("m2", subject="newsletter", labels={"INBOX"})
    client = GmailClient(api)
    j, audit, run_id = _apply_harness(tmp_path)
    # m2's get_meta raises a plain RuntimeError (message genuinely unreadable)
    _inject_get(api, fail_at_call=2, exc=RuntimeError("message gone"))
    result = apply_run(client, _config(), [_cand("m1"), _cand("m2")], j, audit,
                       run_id, confirm=lambda: True)
    assert result == EXIT_PARTIAL
    # m1 applied; m2 recorded as a failure; the loop did NOT abort.
    assert "Cleanup/N" in api.label_names_of("m1")
    assert len(j.failures(run_id)) == 1
    assert j.failures(run_id)[0].startswith("m2: ")


# --- special-category preset query narrowing (Task 42) ----------------------
# old_unread and large_messages have NO meaningful Gmail search term, so
# query_from_match must emit nothing for them — otherwise a pure special-category
# rule would fetch nothing (the bare term "old_unread" never appears in From or
# Subject) and the scan would starve. Text-probe categories keep their bare term.


def test_query_from_match_special_category_emits_no_term():
    assert query_from_match(MatchConfig(category="old_unread")) == ""
    assert query_from_match(MatchConfig(category="large_messages")) == ""


def test_query_from_match_special_category_keeps_other_parts():
    """A mixed rule narrows on its text parts only; the special category itself
    contributes no term."""
    assert query_from_match(
        MatchConfig(category="old_unread", subject_contains=["news"])
    ) == "news"
    assert query_from_match(
        MatchConfig(category="large_messages", from_contains=["sender@example.com"])
    ) == "sender@example.com"


def test_query_from_match_text_probe_categories_unchanged():
    """Text-probe categories still narrow with their bare category term."""
    for cat in ("newsletters", "promotions", "receipts", "notifications"):
        assert query_from_match(MatchConfig(category=cat)) == cat


# --- offline end-to-end scans for the special categories -------------------


def _old_unread_config():
    return Config(
        rules=[
            Rule(id="old", match=MatchConfig(category="old_unread"),
                 actions=Actions(add_label=["Cleanup/Old"], archive=True)),
        ]
    )


def _large_config():
    return Config(
        rules=[
            Rule(id="big", match=MatchConfig(category="large_messages"),
                 actions=Actions(add_label=["Cleanup/Big"], archive=True)),
        ]
    )


def _days_ago_ms(age_days: int) -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000) - age_days * 86_400_000


def test_scan_old_unread_returns_old_unread_only():
    """End-to-end offline scan: a pure old_unread rule must fetch with an EMPTY
    query and locally return ONLY messages that are old AND unread."""
    from gmail_tidy.config import PRESETS
    days = PRESETS["old_unread"]["older_than_days"]
    api = MockGmailApi()
    api.add_message("old_unread", labels={"INBOX"}, unread=True,
                    internal_date_ms=_days_ago_ms(days + 30))
    api.add_message("recent_unread", labels={"INBOX"}, unread=True,
                    internal_date_ms=_days_ago_ms(1))
    api.add_message("old_read", labels={"INBOX"}, unread=False,
                    internal_date_ms=_days_ago_ms(days + 30))
    api.add_message("other", labels={"INBOX"}, unread=False,
                    internal_date_ms=_days_ago_ms(1))
    client = GmailClient(api)
    cands, _cp, _stats = scan(client, _old_unread_config())
    assert [c.message_id for c in cands] == ["old_unread"]


def test_scan_large_messages_returns_large_only():
    """End-to-end offline scan: a pure large_messages rule must fetch with an
    EMPTY query and return only messages at/above the PRESETS size threshold."""
    from gmail_tidy.config import PRESETS
    kb = PRESETS["large_messages"]["larger_than_kb"]
    api = MockGmailApi()
    api.add_message("big", labels={"INBOX"}, size_kb=float(kb) + 500.0)
    api.add_message("at_threshold", labels={"INBOX"}, size_kb=float(kb))
    api.add_message("small", labels={"INBOX"}, size_kb=float(kb) - 1.0)
    cands, _cp, _stats = scan(GmailClient(api), _large_config())
    assert [c.message_id for c in cands] == ["big", "at_threshold"]


def test_scan_special_category_rule_fetches_with_empty_query():
    """A pure special-category rule must drive list_page with an EMPTY query
    (fetch everything, filter locally) — never a bare 'old_unread'/'large_messages'
    term that would match nothing in the mock's From/Subject haystack."""
    from gmail_tidy.config import PRESETS
    days = PRESETS["old_unread"]["older_than_days"]
    api = MockGmailApi()
    api.add_message("old_unread", labels={"INBOX"}, unread=True,
                    internal_date_ms=_days_ago_ms(days + 30))
    client, seen = _spy_list_page(GmailClient(api))
    scan(client, _old_unread_config())
    assert "" in seen  # the rule was fetched with the empty query
    assert "old_unread" not in seen  # the bare term is never emitted
