# tests/test_actions.py
from datetime import datetime, timezone
from pathlib import Path
import pytest
from gmail_tidy.config import Config, PRESETS, Rule, MatchConfig, Actions
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


# --- preset query narrowing (Tasks 42 & 44) ---------------------------------
# Task 42: old_unread and large_messages have NO meaningful Gmail search term,
# so query_from_match must emit nothing for them — otherwise a pure special-
# category rule would fetch nothing and the scan would starve. The special
# categories therefore still contribute no term (and a mixed special-category
# rule narrows on its text parts only).
#
# Task 44: the three text-probe categories WITH a valid Gmail category:
# operator use PRESETS[category]['query'] as their fetch term
# (category:updates / category:promotions / category:purchases) instead of the
# bare category name. notifications has NO valid Gmail category operator, so it
# carries no 'query' in PRESETS and emits no narrowing term — its rules fetch
# everything and filter locally. PRESETS is the single source of truth — the
# emitted term is read from the preset, not hardcoded in this test, so a preset
# query edit cannot silently drift from the code. user MatchConfig.query stays
# ignored.


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


def test_query_from_match_text_probe_uses_preset_query():
    """Each text-probe category with a preset query narrows with
    PRESETS[category]['query'] — never the bare category name."""
    for cat in ("newsletters", "promotions", "receipts"):
        assert PRESETS[cat]["query"]  # these probe presets carry a query
        assert query_from_match(MatchConfig(category=cat)) == PRESETS[cat]["query"]
        assert query_from_match(MatchConfig(category=cat)) != cat


def test_query_from_match_preset_query_matches_expected_operators():
    """The three probe presets with an operator narrow with Gmail category:
    queries; notifications emits NO operator (it has no valid one)."""
    assert query_from_match(MatchConfig(category="newsletters")) == "category:updates"
    assert query_from_match(MatchConfig(category="promotions")) == "category:promotions"
    assert query_from_match(MatchConfig(category="receipts")) == "category:purchases"
    # notifications has no valid Gmail category operator -> empty narrowing
    assert "query" not in PRESETS["notifications"]
    assert query_from_match(MatchConfig(category="notifications")) == ""


def test_query_from_match_notifications_emits_no_term():
    """A pure notifications rule emits an EMPTY query (no preset query key), so
    the scan fetches everything and filters locally via the notifications
    probes — a bare 'notifications' word never appears in From/Subject."""
    assert "query" not in PRESETS["notifications"]
    assert query_from_match(MatchConfig(category="notifications")) == ""


def test_query_from_match_notifications_keeps_other_parts():
    """A mixed notifications rule narrows on its text parts only; the preset
    itself contributes no operator term."""
    assert query_from_match(
        MatchConfig(category="notifications", subject_contains=["alert"])
    ) == "alert"
    assert query_from_match(
        MatchConfig(category="notifications", from_contains=["sender@example.com"])
    ) == "sender@example.com"


def test_query_from_match_ignores_user_query_still():
    """User MatchConfig.query stays ignored even with a preset category."""
    assert query_from_match(
        MatchConfig(category="newsletters", query="user-override")
    ) == PRESETS["newsletters"]["query"]
    # and with a no-operator preset it still contributes nothing
    assert query_from_match(
        MatchConfig(category="notifications", query="user-override")
    ) == ""


# --- multi-term subject/from lists: OR semantics must not AND-narrow (Task 46)
# rules.py matches subject_contains/from_contains with OR semantics (any()).
# Gmail search treats space-separated terms as AND, so emitting both terms of a
# multi-element list would starve messages matching only one of them. A
# multi-element list therefore contributes NO fetch term (fetch more, filter
# locally); a single-element list still narrows, and cross-key single
# subject+from remains AND (both keys are required by the rule check).


def test_query_from_match_multi_subject_emits_no_term():
    """A multi-element subject_contains list (OR in rules.py) must contribute
    no fetch term — Gmail would AND the terms and starve single-term matches."""
    assert query_from_match(MatchConfig(subject_contains=["Newsletter", "Digest"])) == ""
    assert query_from_match(MatchConfig(subject_contains=["a", "b", "c"])) == ""


def test_query_from_match_multi_from_emits_no_term():
    """A multi-element from_contains list (OR in rules.py) must contribute no
    fetch term for the same AND-starvation reason."""
    assert query_from_match(
        MatchConfig(from_contains=["a@example.com", "b@example.com"])
    ) == ""
    assert query_from_match(
        MatchConfig(from_contains=["x@example.com", "y@example.com", "z@example.com"])
    ) == ""


def test_query_from_match_single_term_still_narrows():
    """A single-element list still contributes its term — a lone term cannot
    AND-starve anything, so narrowing stays safe."""
    assert query_from_match(MatchConfig(subject_contains=["newsletter"])) == "newsletter"
    assert query_from_match(
        MatchConfig(from_contains=["sender@example.com"])
    ) == "sender@example.com"


def test_query_from_match_cross_key_single_remains_and():
    """Single subject + single from across keys remains AND: both keys are
    required by the rule check, so both terms narrow the fetch."""
    assert query_from_match(
        MatchConfig(subject_contains=["news"], from_contains=["sender@example.com"])
    ) == "news sender@example.com"


def test_query_from_match_mixed_multi_and_single():
    """A multi-element list in one key contributes nothing while a single
    element in the other key still narrows."""
    assert query_from_match(
        MatchConfig(subject_contains=["Newsletter", "Digest"],
                    from_contains=["sender@example.com"])
    ) == "sender@example.com"
    assert query_from_match(
        MatchConfig(subject_contains=["news"],
                    from_contains=["a@example.com", "b@example.com"])
    ) == "news"


def test_query_from_match_multi_list_with_category_keeps_preset_query():
    """A multi-element text list next to a preset category still narrows with
    the preset's operator query only — the multi list contributes nothing."""
    assert query_from_match(
        MatchConfig(category="newsletters", subject_contains=["Newsletter", "Digest"])
    ) == PRESETS["newsletters"]["query"]
    # a no-operator preset with a multi list emits nothing at all
    assert query_from_match(
        MatchConfig(category="notifications", subject_contains=["Newsletter", "Digest"])
    ) == ""


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


# --- offline end-to-end scans for the text-probe categories (Task 44) -------
# The three probe presets with a valid Gmail category: operator narrow the
# fetch with PRESETS[category]['query']. notifications has no operator, so its
# rules fetch everything (empty query) and local probe matching decides.


def _first_probe(cat: str) -> str:
    """First From/Subject probe text for a probe category (rules._TEXT_PROBES
    is the single source; PRESETS probes stay matched through rules)."""
    from gmail_tidy.rules import _TEXT_PROBES
    return _TEXT_PROBES[cat][0]


def _probe_config(cat: str):
    return Config(
        rules=[Rule(id="r1", match=MatchConfig(category=cat),
                    actions=Actions(add_label=["Cleanup/C"], archive=True))],
    )


def test_scan_text_probe_preset_query_returns_only_probe_matches():
    """End-to-end offline scan: a text-probe category rule must fetch with the
    PRESETS category operator query and return exactly the probe-matching
    messages in that Gmail category — a same-category message with no probe
    text is fetched but locally rejected, and a probe-bearing message in a
    different category is never even fetched."""
    for cat in ("newsletters", "promotions", "receipts"):
        gmail_cat = PRESETS[cat]["query"].split(":", 1)[1]  # e.g. updates
        probe = _first_probe(cat)
        api = MockGmailApi()
        # in the preset's category AND carrying the probe text: the candidate
        api.add_message("probe", category=gmail_cat, labels={"INBOX"},
                        subject=f"{probe} inside")
        # in the category but no probe text: passes the fetch, fails the local
        # probe match -> excluded by the rule check itself
        api.add_message("cat_no_probe", category=gmail_cat, labels={"INBOX"},
                        subject="totally unrelated")
        # probe text but a DIFFERENT category: would match the rule locally but
        # is never fetched under the operator query
        api.add_message("probe_wrong_cat", category="forums", labels={"INBOX"},
                        subject=f"{probe} inside")
        cands, _cp, _stats = scan(GmailClient(api), _probe_config(cat))
        assert [c.message_id for c in cands] == ["probe"], cat


def test_scan_text_probe_category_fetches_with_preset_query():
    """The fetch query scan drives for a text-probe category rule is exactly
    PRESETS[category]['query'] (the operator form) — never the bare name."""
    api = MockGmailApi()
    api.add_message("m1", labels={"INBOX"}, subject="newsletter")
    client, seen = _spy_list_page(GmailClient(api))
    scan(client, _probe_config("newsletters"))
    assert set(seen) == {PRESETS["newsletters"]["query"]}
    assert "newsletters" not in seen  # the bare term is never emitted
    assert "" not in seen  # a preset category never falls back to fetch-everything


def test_scan_notifications_fetches_with_empty_query():
    """A pure notifications rule must drive list_page with an EMPTY query
    (fetch everything, filter locally) — never a bare 'notifications' term and
    never a made-up 'category:notifications' operator."""
    api = MockGmailApi()
    api.add_message("m1", labels={"INBOX"}, subject="alert")
    client, seen = _spy_list_page(GmailClient(api))
    scan(client, _probe_config("notifications"))
    assert "" in seen  # the rule was fetched with the empty query
    assert "notifications" not in seen  # the bare term is never emitted
    assert "category:notifications" not in seen  # the invalid operator is never emitted


def test_scan_notifications_returns_only_probe_matches_across_categories():
    """A pure notifications rule fetches across ANY Gmail category and returns
    only the notification-probe-matching messages — local probe matching alone
    decides eligibility."""
    api = MockGmailApi()
    # notification probe text in 'updates' category: the candidate
    api.add_message("notif_updates", category="updates", labels={"INBOX"},
                    subject="system alert")
    # notification probe text in 'forums' category: also eligible — the local
    # probe match spans categories
    api.add_message("notif_forums", category="forums", labels={"INBOX"},
                    subject="you have a notification")
    # probe text in no category at all: eligible too (no narrowing applied)
    api.add_message("notif_none", category=None, labels={"INBOX"},
                    subject="Alert: action required")
    # no probe text (even with category:updates): rejected locally
    api.add_message("no_probe", category="updates", labels={"INBOX"},
                    subject="totally unrelated")
    cands, _cp, _stats = scan(GmailClient(api), _probe_config("notifications"))
    assert {c.message_id for c in cands} == {"notif_updates", "notif_forums",
                                             "notif_none"}
    # the probe-matching messages were claimed, the unrelated one was not
    assert "no_probe" not in {c.message_id for c in cands}


# --- offline end-to-end scans for multi-term subject/from lists (Task 46) ---
# A multi-element subject_contains/from_contains list is OR in rules.py but
# would be AND in a Gmail query, so it must contribute NO fetch term: the scan
# fetches everything and the local rule check (any()) decides. A single-element
# list still narrows. These scans prove the fix end-to-end: both a
# newsletter-only and a digest-only message are returned by one rule.


def _multi_subject_config():
    return Config(
        rules=[
            Rule(id="r1", match=MatchConfig(subject_contains=["Newsletter", "Digest"]),
                 actions=Actions(add_label=["Cleanup/N"], archive=True)),
        ]
    )


def test_scan_multi_subject_returns_both_single_term_messages():
    """End-to-end offline scan: a rule with a multi-element subject_contains
    list must return BOTH a newsletter-only and a digest-only message — the
    fetch must not AND-narrow to messages containing every term."""
    api = MockGmailApi()
    api.add_message("newsletter_only", subject="Weekly Newsletter", labels={"INBOX"})
    api.add_message("digest_only", subject="Daily Digest", labels={"INBOX"})
    api.add_message("both", subject="Newsletter Digest", labels={"INBOX"})
    api.add_message("neither", subject="Totally unrelated", labels={"INBOX"})
    cands, _cp, _stats = scan(GmailClient(api), _multi_subject_config())
    assert {c.message_id for c in cands} == {"newsletter_only", "digest_only", "both"}
    assert "neither" not in {c.message_id for c in cands}


def test_scan_multi_subject_fetches_with_empty_query():
    """The fetch query for a multi-element subject list is EMPTY (fetch
    everything, filter locally) — never the space-joined AND form."""
    api = MockGmailApi()
    api.add_message("m1", subject="Weekly Newsletter", labels={"INBOX"})
    client, seen = _spy_list_page(GmailClient(api))
    scan(client, _multi_subject_config())
    assert "" in seen  # the rule was fetched with the empty query
    assert "Newsletter Digest" not in seen  # the AND form is never emitted


def _multi_from_config():
    return Config(
        rules=[
            Rule(id="r1", match=MatchConfig(from_contains=["a@example.com", "b@example.com"]),
                 actions=Actions(add_label=["Cleanup/F"], archive=True)),
        ]
    )


def test_scan_multi_from_returns_both_single_term_messages():
    """End-to-end offline scan: a rule with a multi-element from_contains list
    must return messages from EITHER sender — OR semantics preserved."""
    api = MockGmailApi()
    api.add_message("from_a", from_hdr="a@example.com", labels={"INBOX"})
    api.add_message("from_b", from_hdr="b@example.com", labels={"INBOX"})
    api.add_message("from_c", from_hdr="c@example.com", labels={"INBOX"})
    cands, _cp, _stats = scan(GmailClient(api), _multi_from_config())
    assert {c.message_id for c in cands} == {"from_a", "from_b"}
    assert "from_c" not in {c.message_id for c in cands}


def test_scan_multi_from_fetches_with_empty_query():
    """The fetch query for a multi-element from list is EMPTY — never the
    space-joined AND form that would starve single-sender messages."""
    api = MockGmailApi()
    api.add_message("m1", from_hdr="a@example.com", labels={"INBOX"})
    client, seen = _spy_list_page(GmailClient(api))
    scan(client, _multi_from_config())
    assert "" in seen
    assert "a@example.com b@example.com" not in seen


def test_scan_single_term_still_narrows_fetch():
    """A single-element list still narrows the fetch: a message that does not
    contain the lone term is never even fetched."""
    api = MockGmailApi()
    api.add_message("match", subject="newsletter", labels={"INBOX"})
    api.add_message("other", subject="receipt", labels={"INBOX"})
    client, seen = _spy_list_page(GmailClient(api))
    cands, _cp, _stats = scan(client, _config())  # single subject_contains
    assert [c.message_id for c in cands] == ["match"]
    assert set(seen) == {"newsletter"}  # narrowed fetch, not empty


def test_scan_mixed_multi_and_single_keeps_single_narrowing():
    """A multi-element list in one key fetches everything while a single
    element in the other key still narrows — both behaviors compose."""
    cfg = Config(
        rules=[
            Rule(id="r1",
                 match=MatchConfig(subject_contains=["Newsletter", "Digest"],
                                   from_contains=["sender@example.com"]),
                 actions=Actions(add_label=["Cleanup/N"], archive=True)),
        ]
    )
    api = MockGmailApi()
    api.add_message("news_from_sender", subject="Weekly Newsletter",
                    from_hdr="sender@example.com", labels={"INBOX"})
    api.add_message("digest_from_sender", subject="Daily Digest",
                    from_hdr="sender@example.com", labels={"INBOX"})
    api.add_message("news_other_sender", subject="Weekly Newsletter",
                    from_hdr="other@example.com", labels={"INBOX"})
    client, seen = _spy_list_page(GmailClient(api))
    cands, _cp, _stats = scan(client, cfg)
    # only the sender-narrowed messages are candidates; the multi subject list
    # did not AND-narrow them away
    assert {c.message_id for c in cands} == {"news_from_sender", "digest_from_sender"}
    assert set(seen) == {"sender@example.com"}  # single term narrows, multi list does not


def test_scan_multi_subject_with_category_keeps_preset_narrowing():
    """A multi-element subject list next to a preset category still narrows the
    fetch with the preset's operator query only — the multi list contributes
    nothing, so both single-term messages in the category are returned."""
    cfg = Config(
        rules=[
            Rule(id="r1", match=MatchConfig(category="newsletters",
                                            subject_contains=["Newsletter", "Digest"]),
                 actions=Actions(add_label=["Cleanup/N"], archive=True)),
        ]
    )
    api = MockGmailApi()
    api.add_message("news_cat", category="updates", subject="Weekly Newsletter",
                    labels={"INBOX"})
    api.add_message("digest_cat", category="updates", subject="Daily Digest",
                    labels={"INBOX"})
    api.add_message("news_other_cat", category="forums", subject="Weekly Newsletter",
                    labels={"INBOX"})
    client, seen = _spy_list_page(GmailClient(api))
    cands, _cp, _stats = scan(client, cfg)
    assert {c.message_id for c in cands} == {"news_cat", "digest_cat"}
    assert set(seen) == {PRESETS["newsletters"]["query"]}  # category:updates only


# --- overlapping rule queries: seen-dedup must follow the claim (Task 43) --
# scan() previously added every fetched msg_id to the cross-rule `seen` set
# BEFORE evaluating first_matching_rule. When an earlier rule's query fetched a
# message that a LATER rule actually matched (first-match-wins), the earlier
# rule consumed the message into `seen` as excluded/other-rule, and the later
# rule's own pass then skipped it as already-seen — so the message was never
# claimed even though it was eligible under the later rule. The dedup now fires
# only after a rule claims the message (matched.id == rule.id).


def _restrict_then_broader_config():
    """r1 narrows its FETCH to 'newsletter' subjects but its MATCH also demands
    the message be older than 10 days; r2 matches any 'newsletter' subject. Both
    queries are 'newsletter' (identical), so r1's pass fetches messages that
    r1's own match rejects — those must still be claimable by r2."""
    return Config(
        rules=[
            Rule(id="r1", match=MatchConfig(subject_contains=["newsletter"], older_than_days=10),
                 actions=Actions(add_label=["Cleanup/OldNews"], archive=True)),
            Rule(id="r2", match=MatchConfig(subject_contains=["newsletter"]),
                 actions=Actions(add_label=["Cleanup/News"], archive=True)),
        ]
    )


def test_scan_overlap_later_broader_rule_claims_recent_newsletter():
    """A recent newsletter is fetched by r1's query but rejected by r1's own
    (older_than_days) match; first_matching_rule picks r2. The seen-dedup must
    not swallow it under r1 — r2 (the later, broader rule) must claim it."""
    api = MockGmailApi()
    api.add_message("recent", subject="newsletter", labels={"INBOX"},
                    internal_date_ms=_days_ago_ms(1))  # recent: r1's match rejects it
    cands, _cp, stats = scan(GmailClient(api), _restrict_then_broader_config())
    assert [c.message_id for c in cands] == ["recent"]
    assert cands[0].rule_id == "r2"
    # counted once as excluded under r1, then claimed (evaluated) under r2
    assert stats.excluded == 1
    assert stats.candidates == 1
    assert stats.evaluated == stats.noop + stats.candidates


def _old_then_large_config():
    """old_unread first, large_messages second — BOTH fetch with the EMPTY query
    (special categories emit no search term), so every message is fetched under
    both rules' passes."""
    return Config(
        rules=[
            Rule(id="old", match=MatchConfig(category="old_unread"),
                 actions=Actions(add_label=["Cleanup/Old"], archive=True)),
            Rule(id="big", match=MatchConfig(category="large_messages"),
                 actions=Actions(add_label=["Cleanup/Big"], archive=True)),
        ]
    )


def test_scan_overlap_special_categories_claim_large_recent_under_later_rule():
    """old_unread and large_messages both fetch with the empty query (Task 42),
    so a LARGE but RECENT message is fetched under the old_unread pass first,
    rejected there (not old), and must still be claimed by the later
    large_messages rule instead of being swallowed by the seen-dedup."""
    from gmail_tidy.config import PRESETS
    kb = PRESETS["large_messages"]["larger_than_kb"]
    api = MockGmailApi()
    api.add_message("big_recent", labels={"INBOX"}, unread=True,
                    size_kb=float(kb) + 500.0, internal_date_ms=_days_ago_ms(1))
    cands, _cp, _stats = scan(GmailClient(api), _old_then_large_config())
    assert [c.message_id for c in cands] == ["big_recent"]
    assert cands[0].rule_id == "big"


def test_scan_identical_overlapping_rules_first_rule_wins():
    """Two identical rules (same match, same query): first-match-wins must be
    preserved — r1 claims every eligible message and r2 never re-claims one
    (no duplicate candidates), even though r2's query fetches the same pages."""
    cfg = Config(
        rules=[
            Rule(id="r1", match=MatchConfig(subject_contains=["newsletter"]),
                 actions=Actions(add_label=["Cleanup/A"], archive=True)),
            Rule(id="r2", match=MatchConfig(subject_contains=["newsletter"]),
                 actions=Actions(add_label=["Cleanup/B"], archive=True)),
        ]
    )
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    api.add_message("m2", subject="newsletter", labels={"INBOX"})
    cands, _cp, stats = scan(GmailClient(api), cfg)
    assert [c.message_id for c in cands] == ["m1", "m2"]
    assert [c.rule_id for c in cands] == ["r1", "r1"]  # never re-claimed by r2
    assert stats.candidates == 2
    assert stats.excluded == 0


def test_scan_overlap_excluded_message_never_claimed():
    """A protected (excluded) message fetched by BOTH empty-query passes is
    still never claimed by either rule after the seen-move — exclusion stays
    intact across overlapping rule queries."""
    from gmail_tidy.config import PRESETS
    kb = PRESETS["large_messages"]["larger_than_kb"]
    api = MockGmailApi()
    api.add_message("protected_big", labels={"INBOX", "IMPORTANT"},
                    size_kb=float(kb) + 500.0, internal_date_ms=_days_ago_ms(1))
    cands, _cp, _stats = scan(GmailClient(api), _old_then_large_config())
    assert cands == []
