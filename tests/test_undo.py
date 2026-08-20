# tests/test_undo.py
from pathlib import Path
from gmail_tidy.undo import build_undo_plan, execute_undo
from gmail_tidy.audit import Candidate, RunJournal, AuditLog
from gmail_tidy.config import Actions
from gmail_tidy.gmail_client import GmailClient
from gmail_tidy.errors import EXIT_OK
from tests.mock_gmail import MockGmailApi


def _cand() -> Candidate:
    return Candidate(message_id="m1", thread_id="t1", rule_id="r1",
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
    assert "B" in api.store["m1"].label_ids
    assert "INBOX" not in api.store["m1"].label_ids
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
    assert "INBOX" in api.store["m1"].label_ids
    assert "Cleanup/N" not in api.store["m1"].label_ids
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
    assert "INBOX" in api.store["m1"].label_ids
    assert len(audit.entries()) == 2  # add_label + archive from the first undo only
