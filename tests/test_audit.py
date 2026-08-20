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
