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


def test_failures_invalid_utf8_returns_empty(tmp_path):
    """Invalid UTF-8 bytes in a .failures.jsonl degrade to [] — the same as a
    missing file — so summary/apply/run never crash on an undecodable file."""
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    path = tmp_path / "runs" / f"{run_id}.failures.jsonl"
    path.write_bytes(b"\xff\xfe\x00\x00")
    assert j.failures(run_id) == []


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


# --- AuditLog.entries() hardening (Task 41) ---------------------------------
# entries() must never crash scan/apply/undo/run and never leak partial or
# garbage records: blank lines and lines that are not well-formed AuditEntry
# records (malformed JSON, non-objects, missing/wrong-typed fields) are
# skipped; valid records are preserved in file order; a file whose bytes do not
# decode as UTF-8 — and a missing file — both degrade to [] like failures().


def _entry_json(message_id: str = "m1", **overrides) -> dict:
    """A complete, schema-valid audit line (what AuditLog.append writes)."""
    rec = {
        "run_id": "r1", "message_id": message_id, "thread_id": "t1",
        "rule_id": "rule1", "action": "add_label", "payload": "Cleanup/A",
        "kind": "apply", "ts": 1.0,
    }
    rec.update(overrides)
    return rec


def test_entries_missing_file_returns_empty(tmp_path):
    """No audit file yet -> [] (the pre-existing missing-file contract)."""
    assert AuditLog(tmp_path / "audit.jsonl").entries() == []


def test_entries_valid_roundtrip_preserves_order_and_fields(tmp_path):
    """Well-formed records round-trip exactly and stay in file order."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(3):
        log.append(AuditEntry(run_id="r1", message_id=f"m{i}", thread_id=f"t{i}",
                              rule_id="rule1", action="add_label",
                              payload="Cleanup/A", kind="apply", ts=float(i)))
    entries = log.entries()
    assert [e.message_id for e in entries] == ["m0", "m1", "m2"]
    assert entries[0].ts == 0.0 and entries[0].payload == "Cleanup/A"
    assert entries[2].kind == "apply"


def test_entries_blank_lines_skipped(tmp_path):
    """Blank/whitespace-only lines are skipped, never crash."""
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(_entry_json(message_id="m1")) + "\n"
        + "\n"
        + "   \n"
        + json.dumps(_entry_json(message_id="m2")) + "\n",
        encoding="utf-8",
    )
    assert [e.message_id for e in AuditLog(path).entries()] == ["m1", "m2"]


def test_entries_malformed_lines_skipped(tmp_path):
    """Lines that are not valid JSON are skipped without crashing."""
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(_entry_json(message_id="m1")) + "\n"
        + "this is not json{\n"
        + "{broken\n"
        + json.dumps(_entry_json(message_id="m2")) + "\n",
        encoding="utf-8",
    )
    assert [e.message_id for e in AuditLog(path).entries()] == ["m1", "m2"]


@pytest.mark.parametrize("bad_line", [
    "[1, 2, 3]\n",     # valid JSON, not an object
    '"just a string"\n',  # valid JSON, not an object
    "42\n",             # scalar, not an object
    "null\n",           # null, not an object
])
def test_entries_non_object_records_skipped(tmp_path, bad_line):
    """Valid JSON that is not an object is skipped — never crashes."""
    path = tmp_path / "audit.jsonl"
    path.write_text(
        bad_line
        + json.dumps(_entry_json(message_id="m1")) + "\n"
        + bad_line,
        encoding="utf-8",
    )
    assert [e.message_id for e in AuditLog(path).entries()] == ["m1"]


@pytest.mark.parametrize("key,value", [
    ("run_id", 42),              # non-string run_id
    ("message_id", 42),          # non-string message_id
    ("thread_id", None),         # non-string thread_id
    ("rule_id", ["r"]),          # non-string rule_id
    ("action", 1),               # non-string action
    ("kind", True),              # non-string kind
    ("payload", 42),             # payload neither str nor None
    ("payload", ["A"]),          # payload neither str nor None
    ("ts", "yesterday"),         # non-numeric ts
    ("ts", True),                # bool is not a timestamp
])
def test_entries_wrong_type_fields_skipped(tmp_path, key, value):
    """Valid JSON with wrong field types is a corrupt record: skipped, so it
    never flows through as garbage (dataclasses do not type-check)."""
    bad = _entry_json(message_id="bad")
    bad[key] = value
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(bad) + "\n"
        + json.dumps(_entry_json(message_id="m1")) + "\n",
        encoding="utf-8",
    )
    assert [e.message_id for e in AuditLog(path).entries()] == ["m1"]


@pytest.mark.parametrize("missing_key", [
    "run_id", "message_id", "thread_id", "rule_id", "action",
])
def test_entries_missing_required_fields_skipped(tmp_path, missing_key):
    """A record missing a required field (one with no dataclass default) is
    corrupt — skipped, never crashed on."""
    bad = _entry_json(message_id="bad")
    del bad[missing_key]
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(bad) + "\n"
        + json.dumps(_entry_json(message_id="m1")) + "\n",
        encoding="utf-8",
    )
    assert [e.message_id for e in AuditLog(path).entries()] == ["m1"]


def test_entries_missing_optional_fields_use_defaults(tmp_path):
    """Records missing the optional fields (payload/kind/ts, which have
    dataclass defaults) stay valid and are preserved with their defaults —
    hardening must not reject records the dataclass itself round-trips."""
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(_entry_json(message_id="m1", kind="apply", ts=2.0,
                               payload=None)) + "\n",
        encoding="utf-8",
    )
    entry = AuditLog(path).entries()[0]
    assert entry.message_id == "m1"
    assert entry.kind == "apply"
    assert entry.payload is None
    assert entry.ts == 2.0


def test_entries_valid_records_preserved_in_order_around_bad(tmp_path):
    """Bad records anywhere never drop or reorder the valid ones."""
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(_entry_json(message_id="m1")) + "\n"
        + "garbage{{\n"
        + json.dumps(_entry_json(message_id="m2")) + "\n"
        + "\n"
        + "[1, 2]\n"
        + json.dumps(_entry_json(message_id="m3")) + "\n",
        encoding="utf-8",
    )
    assert [e.message_id for e in AuditLog(path).entries()] == ["m1", "m2", "m3"]


def test_entries_invalid_utf8_returns_empty(tmp_path):
    """Invalid UTF-8 bytes degrade to [] — the same as a missing file — so
    scan/undo/run never crash on an undecodable audit file."""
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b"\xff\xfe\x00\x00")
    assert AuditLog(path).entries() == []


def test_entries_invalid_utf8_anywhere_returns_empty(tmp_path):
    """A single undecodable byte anywhere makes the whole file undecodable:
    entries() degrades to [] rather than returning partial data."""
    path = tmp_path / "audit.jsonl"
    path.write_bytes(
        json.dumps(_entry_json(message_id="m1")).encode("utf-8")
        + b"\xff\xfe"
        + json.dumps(_entry_json(message_id="m2")).encode("utf-8")
    )
    assert AuditLog(path).entries() == []
