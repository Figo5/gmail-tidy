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
    assert "Cleanup/N" not in api.label_names_of("m1")
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
    scan(GmailClient(api), _config())
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
