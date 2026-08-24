# tests/test_audit.py
import json

import pytest

from gmail_tidy.audit import AuditLog, AuditEntry, RunJournal, Candidate
from gmail_tidy.config import Actions
from gmail_tidy.errors import ConfigError


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


def test_failures_blank_lines_skipped(tmp_path):
    """Blank/whitespace-only lines in .failures.jsonl are skipped, never crash."""
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    path = tmp_path / "runs" / f"{run_id}.failures.jsonl"
    path.write_text(
        '{"message_id": "m1", "err": "rate limited"}\n'
        "\n"
        "   \n"
        '{"message_id": "m2", "err": "gone"}\n',
        encoding="utf-8",
    )
    assert j.failures(run_id) == ["m1: rate limited", "m2: gone"]


def test_failures_malformed_lines_skipped(tmp_path):
    """Lines that are not valid JSON are skipped without crashing."""
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    j.record_failure(run_id, "m1", "rate limited")
    path = tmp_path / "runs" / f"{run_id}.failures.jsonl"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "this is not json{\n"
        + "{broken\n"
        + '{"message_id": "m2", "err": "gone"}\n',
        encoding="utf-8",
    )
    assert j.failures(run_id) == ["m1: rate limited", "m2: gone"]


def test_failures_partial_objects_skipped(tmp_path):
    """Valid JSON that is not a well-formed {message_id, err} record is skipped
    deterministically — missing keys or wrong types never crash or leak."""
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    path = tmp_path / "runs" / f"{run_id}.failures.jsonl"
    path.write_text(
        '{"message_id": "m1"}\n'                 # missing err
        '{"err": "orphan reason"}\n'             # missing message_id
        '{"message_id": 42, "err": "bad id"}\n'  # non-string message_id
        '[1, 2, 3]\n'                            # not an object
        '"just a string"\n'                      # not an object
        '{"message_id": "m2", "err": "gone"}\n'  # the only valid record
        '{"message_id": "m3", "err": "gone", "extra": 1}\n',
        encoding="utf-8",
    )
    assert j.failures(run_id) == ["m2: gone", "m3: gone"]


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


# --- corrupt run files -> ConfigError / None (Task 35) -----------------------
# load_candidates must convert every JSON/value/key/type/shape parse failure
# into ConfigError(f"run {run_id} is corrupt or unreadable") while PRESERVING
# FileNotFoundError for genuinely missing runs; load_stats must return None for
# corrupt data while preserving missing/valid behavior. Save methods, the
# failures parser, and everything else are untouched.


@pytest.mark.parametrize("bad_content", [
    "{ not json",                       # malformed JSON
    '"just a string"',                  # valid JSON, not a list
    '{"message_id": "m1"}',             # object instead of a list of records
    "[[1, 2, 3]]",                      # list of non-record values
    '{"message_id": "m1"}',             # list element missing thread_id/rule_id/actions
    '[{"message_id": "m1"}]',           # record missing required keys
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": "r1", "actions": "oops", "before_labels": [], "in_inbox": true}]',  # actions not a dict
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": "r1", "actions": {}, "before_labels": "INBOX", "in_inbox": true}]',  # before_labels not a list
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": "r1", "actions": {"add_label": "oops", "archive": true}, "before_labels": [], "in_inbox": true}]',  # add_label not a list
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": "r1", "actions": {"add_label": [], "archive": "yes"}, "before_labels": [], "in_inbox": true}]',  # archive not a bool
    # --- approved Task 35 correction scope: every wrong shape is corrupt -----
    '["m1"]',                           # record is a string, not a dict
    '[[1, 2, 3]]',                      # record is a list, not a dict
    '[{"message_id": 42, "thread_id": "t1", "rule_id": "r1", "actions": {}, "before_labels": [], "in_inbox": true}]',  # message_id not a string
    '[{"message_id": "m1", "thread_id": 42, "rule_id": "r1", "actions": {}, "before_labels": [], "in_inbox": true}]',  # thread_id not a string
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": 42, "actions": {}, "before_labels": [], "in_inbox": true}]',  # rule_id not a string
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": "r1", "actions": {}, "before_labels": {"INBOX": 1}, "in_inbox": true}]',  # before_labels is a dict
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": "r1", "actions": {}, "before_labels": [1], "in_inbox": true}]',  # before_labels non-string element
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": "r1", "actions": {}, "before_labels": ["INBOX", 2], "in_inbox": true}]',  # before_labels mixed
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": "r1", "actions": {}, "before_labels": [], "in_inbox": "yes"}]',  # in_inbox not a bool
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": "r1", "actions": {"add_label": {"a": 1}, "archive": true}, "before_labels": [], "in_inbox": true}]',  # add_label is a dict
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": "r1", "actions": {"add_label": [1], "archive": true}, "before_labels": [], "in_inbox": true}]',  # add_label non-string element
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": "r1", "actions": {"add_label": ["A", 2], "archive": true}, "before_labels": [], "in_inbox": true}]',  # add_label mixed
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": "r1", "actions": {"remove_label": {"a": 1}, "archive": true}, "before_labels": [], "in_inbox": true}]',  # remove_label is a dict
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": "r1", "actions": {"remove_label": [1], "archive": true}, "before_labels": [], "in_inbox": true}]',  # remove_label non-string element
    '[{"message_id": "m1", "thread_id": "t1", "rule_id": "r1", "actions": {"remove_label": ["A", 2], "archive": true}, "before_labels": [], "in_inbox": true}]',  # remove_label mixed
])
def test_load_candidates_corrupt_json_raises_config_error(tmp_path, bad_content):
    """Any JSON/value/key/type/shape failure while reading a run file must raise
    ConfigError with the canonical message — never a raw JSON/KeyError/TypeError."""
    j = RunJournal(tmp_path / "runs")
    run_id = "corruptrun"
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runs" / f"{run_id}.json").write_text(bad_content, encoding="utf-8")
    with pytest.raises(ConfigError, match=f"run {run_id} is corrupt or unreadable"):
        j.load_candidates(run_id)


def test_load_candidates_valid_roundtrip_all_fields(tmp_path):
    """A well-formed run file with every field populated round-trips exactly —
    the hardened validation must not reject valid data."""
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    cand = Candidate(
        message_id="m1", thread_id="t1", rule_id="r1",
        actions=Actions(add_label=["Cleanup/A", "Cleanup/B"],
                        remove_label=["INBOX"], archive=False),
        before_labels={"INBOX", "UNREAD"}, in_inbox=False,
    )
    j.save_candidates(run_id, [cand])
    loaded = j.load_candidates(run_id)
    assert loaded == [cand]
    assert loaded[0].actions.add_label == ["Cleanup/A", "Cleanup/B"]
    assert loaded[0].actions.remove_label == ["INBOX"]
    assert loaded[0].actions.archive is False
    assert loaded[0].before_labels == {"INBOX", "UNREAD"}
    assert loaded[0].in_inbox is False


def test_load_candidates_missing_run_preserves_filenotfound(tmp_path):
    """A genuinely missing run file must still raise FileNotFoundError — the
    CLI depends on it to distinguish 'run not found' from 'run corrupt'."""
    j = RunJournal(tmp_path / "runs")
    with pytest.raises(FileNotFoundError):
        j.load_candidates("no-such-run")


def test_load_candidates_unicode_decode_error_is_config_error(tmp_path):
    """Invalid UTF-8 bytes are a corrupt run too: UnicodeDecodeError -> ConfigError."""
    j = RunJournal(tmp_path / "runs")
    run_id = "bytesrun"
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runs" / f"{run_id}.json").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ConfigError, match=f"run {run_id} is corrupt or unreadable"):
        j.load_candidates(run_id)


@pytest.mark.parametrize("bad_content", [
    "{ not json",         # malformed JSON
    '"just a string"',    # valid JSON, not a dict
    "[1, 2, 3]",          # a list, not a dict
    "42",                 # a scalar
    "null",               # null
])
def test_load_stats_corrupt_returns_none(tmp_path, bad_content):
    """Corrupt stats JSON returns None — same as a run that never saved stats —
    so summary/web degrade gracefully instead of crashing."""
    j = RunJournal(tmp_path / "runs")
    run_id = "corruptstats"
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runs" / f"{run_id}.stats.json").write_text(bad_content, encoding="utf-8")
    assert j.load_stats(run_id) is None


def test_load_stats_unicode_decode_error_returns_none(tmp_path):
    """Invalid UTF-8 bytes in a stats file are treated as absent -> None."""
    j = RunJournal(tmp_path / "runs")
    run_id = "bytesstats"
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runs" / f"{run_id}.stats.json").write_bytes(b"\xff\xfe\x00")
    assert j.load_stats(run_id) is None


def test_load_stats_valid_dict_still_returned(tmp_path):
    """A well-formed stats file keeps returning its dict — corrupt handling must
    not swallow valid data."""
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    stats = {"evaluated": 5, "excluded": 2, "noop": 1, "candidates": 2}
    j.save_stats(run_id, stats)
    assert j.load_stats(run_id) == stats


def test_load_stats_missing_returns_none(tmp_path):
    """No stats file (old run) still returns None."""
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    assert j.load_stats(run_id) is None
