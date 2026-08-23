# tests/test_undo.py
from pathlib import Path
import pytest
from gmail_tidy.undo import build_undo_plan, execute_undo
from gmail_tidy.audit import Candidate, RunJournal, AuditLog
from gmail_tidy.config import Actions
from gmail_tidy.gmail_client import GmailClient
from gmail_tidy.errors import EXIT_OK, AuthError
from tests.mock_gmail import MockGmailApi, _GError


def _cand(mid: str = "m1") -> Candidate:
    return Candidate(message_id=mid, thread_id=f"t-{mid}", rule_id="r1",
                     actions=Actions(add_label=["Cleanup/N"], archive=True),
                     before_labels={"INBOX"}, in_inbox=True)


def test_undo_skips_user_changed_message(tmp_path):
    api = MockGmailApi()
    # apply left: INBOX removed + Cleanup/N added; user then added B manually
    api.add_message("m1", labels={"Cleanup/N", "B"})
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = _cand()
    j.save_candidates(run_id, [cand])
    plan = build_undo_plan(cand)
    result = execute_undo(GmailClient(api), plan, audit, run_id, confirm=lambda: True)
    assert result == EXIT_OK
    # user label B untouched; INBOX NOT re-added (message was user-changed)
    assert "B" in api.label_names_of("m1")
    assert "INBOX" not in api.label_names_of("m1")
    assert not audit.entries()


def test_undo_restores_when_unchanged(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})  # matches left-behind state
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = _cand()
    j.save_candidates(run_id, [cand])
    plan = build_undo_plan(cand)
    result = execute_undo(GmailClient(api), plan, audit, run_id, confirm=lambda: True)
    assert result == EXIT_OK
    assert "INBOX" in api.label_names_of("m1")
    assert "Cleanup/N" not in api.label_names_of("m1")
    entries = audit.entries()
    assert entries and entries[0].kind == "undo"


def test_undo_is_idempotent(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = _cand()
    j.save_candidates(run_id, [cand])
    plan = build_undo_plan(cand)
    execute_undo(GmailClient(api), plan, audit, run_id, confirm=lambda: True)
    execute_undo(GmailClient(api), plan, audit, run_id, confirm=lambda: True)
    # second run: state no longer matches left-behind → skipped, no new entries
    assert "INBOX" in api.label_names_of("m1")
    assert len(audit.entries()) == 2  # add_label + archive from the first undo only


# --- label boundary behavior ---------------------------------------------


def test_undo_never_creates_labels(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    before = api.user_label_names()
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = _cand()
    j.save_candidates(run_id, [cand])
    plan = build_undo_plan(cand)
    execute_undo(GmailClient(api), plan, audit, run_id, confirm=lambda: True)
    # undo only removes labels; the account's label set must be unchanged
    assert api.user_label_names() == before


def test_undo_resolves_ids_at_write_boundary(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = _cand()
    j.save_candidates(run_id, [cand])
    plan = build_undo_plan(cand)
    execute_undo(GmailClient(api), plan, audit, run_id, confirm=lambda: True)
    # the message's stored label_ids are IDs; the undo removed the ID
    assert api.label_id("Cleanup/N") not in api.label_ids_of("m1")


def test_undo_audits_names_not_ids(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = _cand()
    j.save_candidates(run_id, [cand])
    plan = build_undo_plan(cand)
    execute_undo(GmailClient(api), plan, audit, run_id, confirm=lambda: True)
    payloads = [e.payload for e in audit.entries()]
    assert "Cleanup/N" in payloads
    assert "INBOX" in payloads
    assert api.label_id("Cleanup/N") not in payloads


# --- per-message metadata-read failures are skipped (Task 32) --------------
# A message that was deleted, or whose metadata can no longer be read, must
# NOT abort the undo: it is skipped with no write and no audit entry. AuthError
# (403) is the one exception and must propagate for the CLI's auth exit path.


def test_undo_skips_deleted_message_without_aborting(tmp_path):
    """A deleted message (get_meta 404 -> RequestError) is skipped, not fatal."""
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})   # undoable: state still matches
    api.add_message("m2", labels={"Cleanup/N"})   # deleted before undo runs
    del api.store["m2"]                           # Gmail returns 404 -> RequestError
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = _cand("m1")
    cand2 = _cand("m2")
    j.save_candidates(run_id, [cand, cand2])
    plan = build_undo_plan(cand) + build_undo_plan(cand2)
    result = execute_undo(GmailClient(api), plan, audit, run_id, confirm=lambda: True)
    assert result == EXIT_OK
    # m1 undone; deleted m2 skipped with no write and no audit entry
    assert "INBOX" in api.label_names_of("m1")
    entries = audit.entries()
    assert entries and entries[0].kind == "undo"
    assert all(e.message_id == "m1" for e in entries)


def test_undo_skips_message_that_was_never_created(tmp_path):
    """A plan entry whose message never existed must be skipped, not fatal."""
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = _cand("m1")
    cand_ghost = _cand("ghost")  # never created in the mock store
    j.save_candidates(run_id, [cand, cand_ghost])
    plan = build_undo_plan(cand) + build_undo_plan(cand_ghost)
    result = execute_undo(GmailClient(api), plan, audit, run_id, confirm=lambda: True)
    assert result == EXIT_OK
    assert "INBOX" in api.label_names_of("m1")   # healthy message undone
    assert all(e.message_id == "m1" for e in audit.entries())


def test_undo_get_meta_403_propagates_and_not_swallowed(tmp_path):
    """A 403 on get_meta must escape execute_undo as AuthError — the broad
    per-message skip must NOT swallow it — and the failed message must get
    neither a write nor an audit entry."""
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    api.add_message("m2", labels={"Cleanup/N"})
    client = GmailClient(api)
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = _cand("m1")
    cand2 = _cand("m2")
    j.save_candidates(run_id, [cand, cand2])
    plan = build_undo_plan(cand) + build_undo_plan(cand2)
    calls = {"n": 0}
    orig = api._handlers["get"]

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 2:  # m2's metadata read hits a 403
            raise _GError(403, "denied")
        return orig(**kw)

    api._handlers["get"] = flaky
    with pytest.raises(AuthError):
        execute_undo(client, plan, audit, run_id, confirm=lambda: True)
    # m1 (whose read succeeded) was undone; m2's 403 was neither swallowed as
    # a silent skip nor recorded as an audit entry.
    assert "INBOX" in api.label_names_of("m1")
    entries = audit.entries()
    assert all(e.message_id == "m1" for e in entries)
