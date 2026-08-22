# tests/test_audit.py
import json
from gmail_tidy.audit import AuditLog, AuditEntry, RunJournal, Candidate
from gmail_tidy.config import Actions


def test_audit_log_shape(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(AuditEntry(run_id="r1", message_id="m1", thread_id="t1",
                          rule_id="rule1", action="add_label", payload="Cleanup/A"))
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    # fields ONLY — never sender/subject/body/size/content
    assert set(rec) == {"ts", "run_id", "message_id", "thread_id", "rule_id", "action", "payload", "kind"}
    assert "sender" not in json.dumps(rec).lower()


def test_journal_roundtrip_and_failures(tmp_path):
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    cand = Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(add_label=["Cleanup/A"], archive=True),
                     before_labels={"INBOX"}, in_inbox=True)
    j.save_candidates(run_id, [cand])
    loaded = j.load_candidates(run_id)
    assert loaded == [cand]
    assert loaded[0].actions.add_label == ["Cleanup/A"]
    j.record_failure(run_id, "m1", "rate limited")
    assert j.failures(run_id) == ["m1: rate limited"]
    assert run_id in j.list_runs()


def test_save_load_stats_roundtrip_and_missing(tmp_path):
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    stats = {"evaluated": 5, "excluded": 2, "noop": 1, "candidates": 2}
    j.save_stats(run_id, stats)
    assert j.load_stats(run_id) == stats
    other = j.init_run()
    assert j.load_stats(other) is None  # old run with no stats file


def test_list_runs_ignores_stats_files(tmp_path):
    """A saved stats file must not pollute the run journal as a spurious run."""
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    j.save_candidates(run_id, [])
    j.save_stats(run_id, {"evaluated": 0, "excluded": 0, "noop": 0, "candidates": 0})
    assert j.list_runs() == [run_id]
