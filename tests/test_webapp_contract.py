"""Structure tests pinning the web-app design doc to the ACTUAL code contracts.

The v2 web viewer design (docs/superpowers/specs/2026-08-22-gmail-tidy-web-app-design.md)
promises a loopback-only read-only viewer over data that already lives on
disk. These tests lock the design's data surface to reality so the design
cannot silently drift from the code a future web layer will consume:

1. the Candidate / render whitelist the API will serialize;
2. the AuditEntry field set and its on-disk JSONL schema;
3. the checkpoint JSON schema written by save_checkpoint;
4. the OAuth filenames the design excludes ("never read, never served").

Fully offline: no sockets, no Gmail, no config-dir writes outside tmp_path,
no network.
"""

import json
from dataclasses import asdict, fields
from pathlib import Path

from gmail_tidy import auth
from gmail_tidy.audit import AuditEntry, AuditLog, Candidate, RunJournal
from gmail_tidy.checkpoint import (
    RuleCheckpoint,
    ScanCheckpoint,
    config_fingerprint,
    save_checkpoint,
)
from gmail_tidy.config import Actions, Config, MatchConfig, Rule
from gmail_tidy.render import candidate_record

SPEC_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs" / "superpowers" / "specs"
    / "2026-08-22-gmail-tidy-web-app-design.md"
)

# The fields the web layer may serve for one candidate. These names are the
# lockstep contract between render.candidate_record and RunJournal.save_candidates.
CANDIDATE_FIELDS = (
    "message_id", "thread_id", "rule_id", "actions",
    "before_labels", "in_inbox",
)
AUDIT_FIELDS = (
    "run_id", "message_id", "thread_id", "rule_id",
    "action", "payload", "kind", "ts",
)
ACTIONS_FIELDS = ("add_label", "remove_label", "archive")


def _candidate() -> Candidate:
    return Candidate(
        message_id="m1",
        thread_id="t1",
        rule_id="r1",
        actions=Actions(add_label=["Cleanup/A"], archive=True),
        before_labels={"INBOX"},
        in_inbox=True,
    )


def _config() -> Config:
    return Config(
        rules=[
            Rule(id="r1", match=MatchConfig(subject_contains=["newsletter"]),
                 actions=Actions(add_label=["Cleanup/A"], archive=True)),
        ]
    )


# --- Pin 1: Candidate / render whitelist ----------------------------------


def test_candidate_dataclass_field_order():
    assert [f.name for f in fields(Candidate)] == list(CANDIDATE_FIELDS)


def test_actions_dataclass_field_order():
    assert [f.name for f in fields(Actions)] == list(ACTIONS_FIELDS)


def test_candidate_record_is_exact_whitelist():
    rec = candidate_record(_candidate())
    assert set(rec) == set(CANDIDATE_FIELDS)
    assert set(rec["actions"]) == set(ACTIONS_FIELDS)
    # Privacy posture: nothing sender/subject/body/size-shaped may appear.
    assert "sender" not in rec
    assert "subject" not in rec
    assert "body" not in rec
    assert "size" not in rec


def test_candidate_record_matches_saved_run_file_byte_for_byte(tmp_path):
    """candidate_record must equal what RunJournal.save_candidates persists."""
    cand = _candidate()
    journal = RunJournal(tmp_path)
    run_id = journal.init_run()
    journal.save_candidates(run_id, [cand])
    on_disk = json.loads((tmp_path / f"{run_id}.json").read_text(encoding="utf-8"))
    assert on_disk == [candidate_record(cand)]


# Pin 2: AuditEntry fields + serialized schema ------------------------------


def test_audit_entry_field_order():
    assert [f.name for f in fields(AuditEntry)] == list(AUDIT_FIELDS)


def test_audit_entry_serialized_schema_has_no_content_keys():
    entry = AuditEntry(run_id="R", message_id="M", thread_id="T",
                       rule_id="rule-x", action="add_label", payload="Cleanup/A")
    rec = asdict(entry)
    assert set(rec) == set(AUDIT_FIELDS)
    lowered = json.dumps(rec).lower()
    for forbidden in ("sender", "subject", "body", "size"):
        assert forbidden not in lowered


def test_audit_log_persists_exact_schema(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(AuditEntry(run_id="R", message_id="M", thread_id="T",
                          rule_id="rule1", action="add_label", payload="Cleanup/A"))
    rec = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[0])
    assert set(rec) == set(AUDIT_FIELDS)
    assert log.entries()[0].message_id == "M"


# Pin 3: checkpoint serialized schema ---------------------------------------


def test_checkpoint_schema_matches_save_checkpoint(tmp_path):
    cfg = _config()
    cp = ScanCheckpoint(
        config_fingerprint=config_fingerprint(cfg),
        rules={"r1": RuleCheckpoint(page_token="page-token-1", exhausted=True)},
    )
    path = tmp_path / "checkpoint.json"
    save_checkpoint(path, cp)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"config_fingerprint", "rules"}
    assert set(data["rules"]) == {"r1"}
    assert set(data["rules"]["r1"]) == {"page_token", "exhausted"}
    assert data["rules"]["r1"] == {"page_token": "page-token-1", "exhausted": True}
    # Round-trip through load_checkpoint preserves the pinned schema.
    loaded = _load_checkpoint(path, cfg)
    assert loaded.config_fingerprint == cp.config_fingerprint
    assert loaded.rules["r1"].page_token == "page-token-1"
    assert loaded.rules["r1"].exhausted is True


def _load_checkpoint(path: Path, config: Config):
    from gmail_tidy.checkpoint import load_checkpoint
    return load_checkpoint(path, config)


# Pin 4: secret exclusion ----------------------------------------------------


def test_token_and_secret_names_are_excluded_by_design():
    # The names the design promises to "never read / never serve" must be the
    # real filenames auth.py uses, so the exclusion promise is not about a
    # typo'd string.
    assert auth.TOKEN_NAME == "token.json"
    assert auth.SECRET_NAME == "client_secret.json"
    text = SPEC_PATH.read_text(encoding="utf-8")
    assert "token.json" in text
    assert "client_secret" in text
    # The design must say they are never read/served.
    assert "never read" in text or "Never opened" in text
    assert "excluded" in text


def test_design_acknowledges_v1_non_goal_and_plan_collision():
    text = SPEC_PATH.read_text(encoding="utf-8")
    assert "No web UI" in text  # quotes the v1 spec non-goal rather than editing it
    assert "Scan and apply" in text  # names the pre-existing v1 Task 7
    assert "collision" in text.lower()  # acknowledges the numbering clash
