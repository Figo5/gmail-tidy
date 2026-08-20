# tests/test_actions.py
from pathlib import Path
from gmail_tidy.config import Config, Rule, MatchConfig, Actions
from gmail_tidy.actions import scan, apply_run
from gmail_tidy.audit import RunJournal, AuditLog, Candidate
from gmail_tidy.gmail_client import GmailClient
from gmail_tidy.errors import EXIT_OK, EXIT_CANCELLED
from tests.mock_gmail import MockGmailApi


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
    c = scan(GmailClient(api), _config())
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
    assert "Cleanup/N" not in api.store["m1"].label_ids
    assert not audit.entries()


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
    assert "Cleanup/N" in api.store["m1"].label_ids
    assert "INBOX" not in api.store["m1"].label_ids


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
