"""Offline tests for the loopback-only read-only web viewer (Task 8).

Covers the pure ``web.handle(method, path, cfg_dir)`` seam, privacy/secret
exclusion, method/route/run-id validation, checkpoint projection, audit
limits and summary, response headers, an in-process loopback server smoke
test, and the CLI ``web`` command seam. Fully offline: no sockets except a
single loopback server in one test (bound to 127.0.0.1), no Gmail, no OAuth,
no network, and the CLI web command is driven with --no-browser so it never
touches webbrowser.
"""

import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gmail_tidy import web
from gmail_tidy.audit import AuditEntry, RunJournal
from gmail_tidy.cli import app
from gmail_tidy.config import Actions

runner = CliRunner()

CONFIG_TEXT = (
    "rules:\n"
    "  - id: r1\n"
    "    match: {subject_contains: [newsletter], older_than_days: 30}\n"
    "    actions:\n"
    "      add_label: [Cleanup/N]\n"
    "      archive: true\n"
)

CHECKPOINT_DOC = {
    "config_fingerprint": "fp1234",
    "rules": {
        "r1": {"page_token": None, "exhausted": True},
        "r2": {"page_token": "super-secret-page-token", "exhausted": False},
    },
}


def _candidate(message_id="m1", thread_id="t1", rule_id="r1"):
    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "rule_id": rule_id,
        "actions": {"add_label": ["Cleanup/N"], "remove_label": [], "archive": True},
        "before_labels": ["INBOX"],
        "in_inbox": True,
    }


def _populate(tmp_path: Path) -> dict:
    """Build a realistic config dir and return its known values.

    Returns the run id and the exact audit entry used so tests can assert on
    the data the viewer should project.
    """
    (tmp_path / "config.yaml").write_text(CONFIG_TEXT, encoding="utf-8")
    journal = RunJournal(tmp_path / "runs")
    run_id = journal.init_run()
    cand = {"message_id": "m1", "thread_id": "t1", "rule_id": "r1",
            "actions": Actions(add_label=["Cleanup/N"], archive=True),
            "before_labels": {"INBOX"}, "in_inbox": True}
    from gmail_tidy.audit import Candidate
    journal.save_candidates(run_id, [Candidate(**cand)])
    journal.save_stats(run_id, {"evaluated": 3, "excluded": 1, "noop": 1, "candidates": 1})
    audit_entry = {
        "run_id": run_id, "message_id": "m1", "thread_id": "t1", "rule_id": "r1",
        "action": "add_label", "payload": "Cleanup/N", "kind": "apply", "ts": 1720000000.0,
    }
    (tmp_path / "audit.jsonl").write_text(json.dumps(audit_entry) + "\n", encoding="utf-8")
    (tmp_path / "checkpoint.json").write_text(json.dumps(CHECKPOINT_DOC), encoding="utf-8")
    (tmp_path / "token.json").write_text(
        json.dumps({"token": "SECRET-ACCESS-TOKEN", "scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}),
        encoding="utf-8",
    )
    (tmp_path / "client_secret.json").write_text(
        json.dumps({"installed": {"client_secret": "SUPER-SECRET"}}), encoding="utf-8")
    return {"run_id": run_id}


def _body(response: web.Response) -> dict:
    return json.loads(response.body)


# ---------------------------------------------------------------------------
# Pure handler routes
# ---------------------------------------------------------------------------


def test_healthz_and_health(tmp_path):
    for route in ("/healthz", "/api/v1/health"):
        resp = web.handle("GET", route, tmp_path)
        assert resp.status == 200
        assert _body(resp) == {"status": "ok"}
        assert resp.extra_headers == {}


def test_root_serves_html_shell(tmp_path):
    resp = web.handle("GET", "/", tmp_path)
    assert resp.status == 200
    assert resp.content_type == "text/html; charset=utf-8"
    assert b"<html" in resp.body
    assert b"gmail-tidy" in resp.body


def test_status_projection(tmp_path):
    info = _populate(tmp_path)
    resp = web.handle("GET", "/api/v1/status", tmp_path)
    assert resp.status == 200
    body = _body(resp)
    assert body["config_present"] is True
    assert body["config_valid"] is True
    assert body["token_present"] is True
    assert body["scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert body["checkpoint_present"] is True
    assert body["runs_count"] == 1
    assert body["latest_run"] == info["run_id"]
    assert body["config_dir"] == str(tmp_path)


def test_status_invalid_config_reports_invalid(tmp_path):
    (tmp_path / "config.yaml").write_text("rules: [{bad", encoding="utf-8")
    resp = web.handle("GET", "/api/v1/status", tmp_path)
    body = _body(resp)
    assert body["config_present"] is True
    assert body["config_valid"] is False


def test_status_empty_dir(tmp_path):
    resp = web.handle("GET", "/api/v1/status", tmp_path)
    body = _body(resp)
    assert body == {
        "config_dir": str(tmp_path), "config_present": False, "config_valid": False,
        "token_present": False, "scopes": [], "checkpoint_present": False,
        "runs_count": 0, "latest_run": None,
    }


def test_status_wrong_shape_token_scopes_empty_200(tmp_path):
    """A valid-JSON-but-wrong-shaped token must degrade to scopes [] — the
    status route returns 200, never a 500 envelope from a raw
    AttributeError/TypeError in scope_state."""
    _populate(tmp_path)
    (tmp_path / "token.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    resp = web.handle("GET", "/api/v1/status", tmp_path)
    assert resp.status == 200
    body = _body(resp)
    assert body["token_present"] is True
    assert body["scopes"] == []


def test_status_scopes_string_token_scopes_empty_200(tmp_path):
    _populate(tmp_path)
    (tmp_path / "token.json").write_text('{"scopes": "readonly"}', encoding="utf-8")
    resp = web.handle("GET", "/api/v1/status", tmp_path)
    assert resp.status == 200
    assert _body(resp)["scopes"] == []


def test_status_mixed_scopes_token_scopes_empty_200(tmp_path):
    _populate(tmp_path)
    (tmp_path / "token.json").write_text(
        json.dumps({"scopes": ["https://www.googleapis.com/auth/gmail.readonly", 42]}),
        encoding="utf-8",
    )
    resp = web.handle("GET", "/api/v1/status", tmp_path)
    assert resp.status == 200
    assert _body(resp)["scopes"] == []


def test_config_projection_criteria_only(tmp_path):
    _populate(tmp_path)
    resp = web.handle("GET", "/api/v1/config", tmp_path)
    assert resp.status == 200
    body = _body(resp)
    assert len(body["rules"]) == 1
    rule = body["rules"][0]
    assert rule["id"] == "r1"
    criteria = {c["name"]: c["value"] for c in rule["criteria"]}
    assert criteria["subject_contains"] == ["newsletter"]
    assert criteria["older_than_days"] == 30
    # actions / raw yaml never appear
    assert "actions" not in rule
    assert "add_label" not in json.dumps(body)


def test_config_404_when_missing(tmp_path):
    resp = web.handle("GET", "/api/v1/config", tmp_path)
    assert resp.status == 404


def test_status_malformed_rules_shape_config_valid_false(tmp_path):
    """A non-list `rules:` value must report config_valid false (200), never a
    500 — before Task 39 the IndexError in _format_errors escaped as internal
    error."""
    (tmp_path / "config.yaml").write_text("rules: notalist\n", encoding="utf-8")
    resp = web.handle("GET", "/api/v1/status", tmp_path)
    assert resp.status == 200
    body = _body(resp)
    assert body["config_present"] is True
    assert body["config_valid"] is False


def test_config_404_on_malformed_rules_shape(tmp_path):
    """An invalid config must 404 /api/v1/config, never 500 (Task 39)."""
    (tmp_path / "config.yaml").write_text("rules: notalist\n", encoding="utf-8")
    resp = web.handle("GET", "/api/v1/config", tmp_path)
    assert resp.status == 404


def test_runs_projection(tmp_path):
    info = _populate(tmp_path)
    resp = web.handle("GET", "/api/v1/runs", tmp_path)
    body = _body(resp)
    assert body == {"latest": info["run_id"], "runs": [info["run_id"]]}


def test_run_detail_projection(tmp_path):
    info = _populate(tmp_path)
    resp = web.handle("GET", f"/api/v1/runs/{info['run_id']}", tmp_path)
    assert resp.status == 200
    body = _body(resp)
    assert body["run"] == info["run_id"]
    cand = body["candidates"][0]
    assert cand == _candidate()
    # stats included
    assert body["stats"]["evaluated"] == 3
    # stats never contain message ids, only aggregates
    assert "message_id" not in json.dumps(body["stats"])


def test_run_detail_unknown_run(tmp_path):
    resp = web.handle("GET", "/api/v1/runs/deadbeef1234", tmp_path)
    assert resp.status == 404


# ---------------------------------------------------------------------------
# Corrupt run detail files (Task 45)
# ---------------------------------------------------------------------------


def test_run_detail_malformed_json_404_not_500(tmp_path):
    """A run file that is not valid JSON must 404 the run, never a 500 from a
    raw JSONDecodeError escaping _run_projection."""
    info = _populate(tmp_path)
    (tmp_path / "runs" / f"{info['run_id']}.json").write_text(
        "{not-json", encoding="utf-8")
    resp = web.handle("GET", f"/api/v1/runs/{info['run_id']}", tmp_path)
    assert resp.status == 404


def test_run_detail_wrong_shape_404_not_500(tmp_path):
    """A valid-JSON run file with the wrong shape (records must be objects)
    must 404 the run, never a 500."""
    info = _populate(tmp_path)
    (tmp_path / "runs" / f"{info['run_id']}.json").write_text(
        json.dumps([1, 2, 3]), encoding="utf-8")
    resp = web.handle("GET", f"/api/v1/runs/{info['run_id']}", tmp_path)
    assert resp.status == 404


def test_run_detail_invalid_utf8_404_not_500(tmp_path):
    """Invalid UTF-8 bytes in a run file must 404 the run, never a 500 from a
    raw UnicodeDecodeError escaping from _run_projection."""
    info = _populate(tmp_path)
    (tmp_path / "runs" / f"{info['run_id']}.json").write_bytes(b"\xff\xfe\x00\x00")
    resp = web.handle("GET", f"/api/v1/runs/{info['run_id']}", tmp_path)
    assert resp.status == 404


def test_run_detail_valid_regression_after_corruption_hardening(tmp_path):
    """A well-formed run file keeps serving its detail after the corruption
    hardening — valid runs must not be rejected."""
    info = _populate(tmp_path)
    resp = web.handle("GET", f"/api/v1/runs/{info['run_id']}", tmp_path)
    assert resp.status == 200
    assert _body(resp)["run"] == info["run_id"]


def test_run_detail_missing_regression_after_corruption_hardening(tmp_path):
    """A missing run file still 404s after the corruption hardening."""
    _populate(tmp_path)
    resp = web.handle("GET", "/api/v1/runs/deadbeef1234", tmp_path)
    assert resp.status == 404


def test_audit_projection_and_limit(tmp_path):
    info = _populate(tmp_path)
    resp = web.handle("GET", "/api/v1/audit", tmp_path)
    assert resp.status == 200
    entries = _body(resp)["entries"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["run_id"] == info["run_id"]
    assert entry["message_id"] == "m1"
    # clamped at AUDIT_MAX_LIMIT
    resp = web.handle("GET", "/api/v1/audit?limit=999999", tmp_path)
    assert len(_body(resp)["entries"]) == 1


def test_audit_limit_zero_or_negative_is_400(tmp_path):
    _populate(tmp_path)
    for q in ("limit=0", "limit=-3"):
        resp = web.handle("GET", f"/api/v1/audit?{q}", tmp_path)
        assert resp.status == 400


def test_audit_limit_non_int_is_400(tmp_path):
    _populate(tmp_path)
    resp = web.handle("GET", "/api/v1/audit?limit=abc", tmp_path)
    assert resp.status == 400


def test_audit_empty_dir(tmp_path):
    resp = web.handle("GET", "/api/v1/audit", tmp_path)
    assert resp.status == 200
    assert _body(resp) == {"entries": []}


def test_audit_corrupt_line_skipped(tmp_path):
    (tmp_path / "audit.jsonl").write_text(
        "not-json\n" + json.dumps({
            "run_id": "x", "message_id": "m1", "thread_id": "t1", "rule_id": "r1",
            "action": "archive", "payload": None, "kind": "apply", "ts": 1.0,
        }) + "\n",
        encoding="utf-8",
    )
    resp = web.handle("GET", "/api/v1/audit", tmp_path)
    assert resp.status == 200
    assert len(_body(resp)["entries"]) == 1


def test_audit_summary(tmp_path):
    info = _populate(tmp_path)
    resp = web.handle("GET", "/api/v1/audit/summary", tmp_path)
    body = _body(resp)
    assert body["by_rule"] == {"r1": 1}
    assert body["by_action"] == {"add_label": 1}
    assert body["by_kind"] == {"apply": 1}


def test_audit_summary_no_message_ids(tmp_path):
    _populate(tmp_path)
    resp = web.handle("GET", "/api/v1/audit/summary", tmp_path)
    body = _body(resp)
    assert "m1" not in json.dumps(body)
    assert "thread" not in json.dumps(body)


def test_checkpoint_projection_never_page_tokens(tmp_path):
    _populate(tmp_path)
    resp = web.handle("GET", "/api/v1/checkpoint", tmp_path)
    body = _body(resp)
    assert body["fingerprint"] == "fp1234"
    assert body["rules"] == {"r1": "exhausted", "r2": "in-progress"}
    # the page token must never leak
    assert "super-secret-page-token" not in resp.body.decode()


def test_checkpoint_missing_or_corrupt_degrades(tmp_path):
    resp = web.handle("GET", "/api/v1/checkpoint", tmp_path)
    assert resp.status == 200
    assert _body(resp) == {"fingerprint": None, "rules": {}}
    (tmp_path / "checkpoint.json").write_text("garbage", encoding="utf-8")
    resp = web.handle("GET", "/api/v1/checkpoint", tmp_path)
    assert _body(resp) == {"fingerprint": None, "rules": {}}


# ---------------------------------------------------------------------------
# Invalid UTF-8 bytes (Task 40)
# ---------------------------------------------------------------------------


def test_checkpoint_invalid_utf8_degrades_empty_200(tmp_path):
    """Invalid UTF-8 bytes in checkpoint.json: empty projection, 200 — never a
    500 from a raw UnicodeDecodeError escaping _checkpoint_projection."""
    (tmp_path / "checkpoint.json").write_bytes(b"\xff\xfe\x00\x00")
    response = web.handle("GET", "/api/v1/checkpoint", tmp_path)
    assert response.status == 200
    assert _body(response) == {"fingerprint": None, "rules": {}}


def test_audit_invalid_utf8_degrades_empty_200(tmp_path):
    """Invalid UTF-8 bytes in audit.jsonl: empty entries, 200 — never a 500."""
    (tmp_path / "audit.jsonl").write_bytes(b"\xff\xfe\x00\x00")
    response = web.handle("GET", "/api/v1/audit", tmp_path)
    assert response.status == 200
    assert _body(response) == {"entries": []}


def test_audit_summary_invalid_utf8_degrades_empty_200(tmp_path):
    (tmp_path / "audit.jsonl").write_bytes(b"\xff\xfe\x00\x00")
    response = web.handle("GET", "/api/v1/audit/summary", tmp_path)
    assert response.status == 200
    assert _body(response) == {"by_rule": {}, "by_action": {}, "by_kind": {}}


def test_status_invalid_utf8_config_and_token_200(tmp_path):
    """Invalid UTF-8 config.yaml/token.json degrade to config_valid false and
    scopes [] respectively — the status route stays 200, never a 500."""
    (tmp_path / "config.yaml").write_bytes(b"\xff\xfe\x00\x00")
    (tmp_path / "token.json").write_bytes(b"\xff\xfe\x00\x00")
    response = web.handle("GET", "/api/v1/status", tmp_path)
    assert response.status == 200
    body = _body(response)
    assert body["config_present"] is True
    assert body["config_valid"] is False
    assert body["token_present"] is True
    assert body["scopes"] == []


def test_config_404_on_invalid_utf8(tmp_path):
    """An undecodable config.yaml must 404 /api/v1/config (invalid config is
    'not found' for the viewer), never 500."""
    (tmp_path / "config.yaml").write_bytes(b"\xff\xfe\x00\x00")
    response = web.handle("GET", "/api/v1/config", tmp_path)
    assert response.status == 404


def test_valid_checkpoint_file_regression_after_hardening(tmp_path):
    """A well-formed checkpoint.json keeps projecting fingerprint + rule status —
    the invalid-UTF-8 hardening must not reject valid data."""
    (tmp_path / "checkpoint.json").write_text(
        json.dumps({"config_fingerprint": "abc", "rules": {"r1": {"page_token": None, "exhausted": True}}}),
        encoding="utf-8",
    )
    response = web.handle("GET", "/api/v1/checkpoint", tmp_path)
    assert response.status == 200
    assert _body(response) == {"fingerprint": "abc", "rules": {"r1": "exhausted"}}


def test_valid_audit_regression_after_hardening(tmp_path):
    """A well-formed audit.jsonl keeps serving its entries after the
    invalid-UTF-8 hardening."""
    (tmp_path / "audit.jsonl").write_text(
        json.dumps({"run_id": "r", "message_id": "m1", "thread_id": "t1",
                    "rule_id": "rule1", "action": "add_label", "payload": "Cleanup/A",
                    "kind": "apply", "ts": 1.0}) + "\n",
        encoding="utf-8",
    )
    response = web.handle("GET", "/api/v1/audit", tmp_path)
    assert response.status == 200
    assert len(_body(response)["entries"]) == 1


# ---------------------------------------------------------------------------
# Audit entry type validation (Task 47)
#
# _read_audit_entries must mirror AuditLog.entries' type validation without
# touching AuditLog (whose __init__ performs a write) and without any write of
# its own: non-dict records, missing/non-string required fields, wrong-typed
# optional fields (payload/kind/ts), and ts bool are shape errors -> skipped;
# an integral ts is normalized to float; unknown extra keys are dropped (only
# the known fields are passed through); valid records keep their file order.
# ---------------------------------------------------------------------------


def _audit_line(overrides=None):
    """A well-formed audit record as one JSONL line, with optional overrides."""
    rec = {
        "run_id": "r", "message_id": "m1", "thread_id": "t1", "rule_id": "rule1",
        "action": "add_label", "payload": "Cleanup/A", "kind": "apply", "ts": 1720000000.0,
    }
    if overrides:
        rec.update(overrides)
    return json.dumps(rec)


@pytest.mark.parametrize("field", ["run_id", "message_id", "thread_id", "rule_id", "action"])
def test_audit_required_field_wrong_type_skipped(tmp_path, field):
    """A required field that is present but NOT a string is a shape error: the
    record is skipped, never projected with an unvalidated value. AuditEntry is
    an unvalidated dataclass, so the pre-Task-47 reader let e.g. run_id=42 flow
    straight through into the response."""
    (tmp_path / "audit.jsonl").write_text(
        _audit_line({field: 42}) + "\n", encoding="utf-8")
    assert _body(web.handle("GET", "/api/v1/audit", tmp_path)) == {"entries": []}
    assert _body(web.handle("GET", "/api/v1/audit/summary", tmp_path)) == {
        "by_rule": {}, "by_action": {}, "by_kind": {}}


@pytest.mark.parametrize("field", ["run_id", "message_id", "thread_id", "rule_id", "action"])
def test_audit_missing_required_field_skipped(tmp_path, field):
    """A required field that is absent is a shape error: skipped."""
    rec = {
        "run_id": "r", "message_id": "m1", "thread_id": "t1", "rule_id": "rule1",
        "action": "add_label",
    }
    rec.pop(field)
    (tmp_path / "audit.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    resp = web.handle("GET", "/api/v1/audit", tmp_path)
    assert resp.status == 200
    assert _body(resp) == {"entries": []}


def test_audit_payload_non_string_non_none_skipped(tmp_path):
    """payload must be a string or None — anything else is a shape error."""
    (tmp_path / "audit.jsonl").write_text(
        _audit_line({"payload": 123}) + "\n", encoding="utf-8")
    assert _body(web.handle("GET", "/api/v1/audit", tmp_path)) == {"entries": []}


def test_audit_kind_non_string_skipped(tmp_path):
    (tmp_path / "audit.jsonl").write_text(
        _audit_line({"kind": ["apply"]}) + "\n", encoding="utf-8")
    assert _body(web.handle("GET", "/api/v1/audit", tmp_path)) == {"entries": []}


@pytest.mark.parametrize("bad_ts", ["1720000000", None, [1.0]])
def test_audit_ts_non_numeric_skipped(tmp_path, bad_ts):
    """ts must be numeric; a string/None/list is a shape error."""
    (tmp_path / "audit.jsonl").write_text(
        _audit_line({"ts": bad_ts}) + "\n", encoding="utf-8")
    assert _body(web.handle("GET", "/api/v1/audit", tmp_path)) == {"entries": []}


def test_audit_ts_bool_skipped(tmp_path):
    """bool is an int subclass but not a timestamp — skipped explicitly."""
    (tmp_path / "audit.jsonl").write_text(
        _audit_line({"ts": True}) + "\n", encoding="utf-8")
    assert _body(web.handle("GET", "/api/v1/audit", tmp_path)) == {"entries": []}


def test_audit_ts_int_normalized_to_float(tmp_path):
    """An integral ts is stored as float, mirroring AuditLog.entries' float(ts)."""
    (tmp_path / "audit.jsonl").write_text(
        _audit_line({"ts": 1720000000}) + "\n", encoding="utf-8")
    entries = _body(web.handle("GET", "/api/v1/audit", tmp_path))["entries"]
    assert len(entries) == 1
    assert entries[0]["ts"] == 1720000000.0
    assert type(entries[0]["ts"]) is float


def test_audit_extra_keys_dropped_record_preserved(tmp_path):
    """Unknown keys are not part of the pinned schema: they must be dropped
    (only the known fields are passed to AuditEntry), not reject the whole
    record — and never leak into the response."""
    (tmp_path / "audit.jsonl").write_text(
        _audit_line({"subject": "spam", "content": "secret"}) + "\n",
        encoding="utf-8")
    resp = web.handle("GET", "/api/v1/audit", tmp_path)
    assert resp.status == 200
    entries = _body(resp)["entries"]
    assert len(entries) == 1
    assert set(entries[0]) == {"run_id", "message_id", "thread_id", "rule_id",
                               "action", "payload", "kind", "ts"}
    lowered = json.dumps(entries)
    assert "subject" not in lowered
    assert "secret" not in lowered


def test_audit_interleaving_preserves_valid_order(tmp_path):
    """Valid records keep their file order around records that are skipped as
    shape errors (corrupt JSON, non-dict, wrong-typed fields, bool ts)."""
    good = _audit_line({"run_id": "r1"})
    bad_ts = _audit_line({"ts": True})
    good_mid = _audit_line({"run_id": "r2"})
    (tmp_path / "audit.jsonl").write_text(
        good + "\n" + "not-json\n" + bad_ts + "\n" + good_mid + "\n"
        + "[1,2,3]\n" + "null\n", encoding="utf-8")
    entries = _body(web.handle("GET", "/api/v1/audit", tmp_path))["entries"]
    assert [e["run_id"] for e in entries] == ["r1", "r2"]
    summary = _body(web.handle("GET", "/api/v1/audit/summary", tmp_path))
    assert summary == {"by_rule": {"rule1": 2},
                       "by_action": {"add_label": 2},
                       "by_kind": {"apply": 2}}


def test_audit_summary_string_keys_after_type_validation(tmp_path):
    """Records with non-string rule_id/action/kind must not pollute the summary
    with coerced keys — only real string keys appear."""
    (tmp_path / "audit.jsonl").write_text(
        _audit_line({"run_id": 123, "action": 7, "kind": 99}) + "\n"
        + _audit_line() + "\n", encoding="utf-8")
    summary = _body(web.handle("GET", "/api/v1/audit/summary", tmp_path))
    assert summary == {"by_rule": {"rule1": 1},
                       "by_action": {"add_label": 1},
                       "by_kind": {"apply": 1}}
    assert all(isinstance(k, str) for k in summary["by_rule"])
    assert all(isinstance(k, str) for k in summary["by_action"])
    assert all(isinstance(k, str) for k in summary["by_kind"])
    assert "123" not in json.dumps(summary)


def test_audit_valid_record_unchanged_after_type_validation(tmp_path):
    """A fully valid record projects exactly as before the Task 47 hardening —
    same values, same pinned field set, same response schema."""
    (tmp_path / "audit.jsonl").write_text(
        _audit_line() + "\n", encoding="utf-8")
    resp = web.handle("GET", "/api/v1/audit", tmp_path)
    assert resp.status == 200
    assert _body(resp) == {"entries": [json.loads(_audit_line())]}
    summary = _body(web.handle("GET", "/api/v1/audit/summary", tmp_path))
    assert summary == {"by_rule": {"rule1": 1},
                       "by_action": {"add_label": 1},
                       "by_kind": {"apply": 1}}


# ---------------------------------------------------------------------------
# Privacy / secret exclusion
# ---------------------------------------------------------------------------


def test_token_and_client_secret_bytes_never_served(tmp_path):
    info = _populate(tmp_path)
    for route in ("/api/v1/status", "/api/v1/audit", "/api/v1/audit/summary",
                  "/api/v1/checkpoint", "/api/v1/config", "/api/v1/runs",
                  f"/api/v1/runs/{info['run_id']}"):
        resp = web.handle("GET", route, tmp_path)
        lowered = resp.body.decode()
        assert "secret-access-token" not in lowered
        assert "super-secret" not in lowered


def test_token_file_bytes_not_leaked_by_any_endpoint(tmp_path):
    """The token file's credential material must never appear in any output."""
    _populate(tmp_path)
    token_bytes = (tmp_path / "token.json").read_bytes()
    for route in ("/healthz", "/api/v1/status", "/api/v1/audit",
                  "/api/v1/audit/summary", "/api/v1/checkpoint", "/api/v1/config",
                  "/api/v1/runs"):
        resp = web.handle("GET", route, tmp_path)
        assert token_bytes not in resp.body


def test_token_scopes_only_names(tmp_path):
    _populate(tmp_path)
    resp = web.handle("GET", "/api/v1/status", tmp_path)
    assert "scopes" in _body(resp)
    assert "https://www.googleapis.com/auth/gmail.readonly" in resp.body.decode()


# ---------------------------------------------------------------------------
# Method / route / run-id validation
# ---------------------------------------------------------------------------


def test_non_get_is_405(tmp_path):
    _populate(tmp_path)
    for method in ("POST", "PUT", "DELETE"):
        resp = web.handle(method, "/api/v1/status", tmp_path)
        assert resp.status == 405
        assert _body(resp) == {"error": "method not allowed"}


def test_unknown_routes_are_404(tmp_path):
    _populate(tmp_path)
    for route in ("/api/v1/other", "/api/v1/runs/",
                  "/api/v1/runs/deadbeef12", "/api/v1/runs/deadbeef1234/extra",
                  "/api/v1/runs/../../etc/passwd", "/runs/DEADBEEF1234",
                  "/api/v1/runs/DEADBEEF1234", "/api/v1/runs/deadbeef12345"):
        resp = web.handle("GET", route, tmp_path)
        assert resp.status == 404, route


def test_run_id_validated_before_filesystem_access(tmp_path):
    """An invalid run id must 404 without touching the filesystem."""
    _populate(tmp_path)
    resp = web.handle("GET", "/api/v1/runs/not-a-run-id", tmp_path)
    assert resp.status == 404


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def test_json_responses_have_no_store_no_cors_no_cookies(tmp_path):
    resp = web.handle("GET", "/api/v1/status", tmp_path)
    # The pure seam carries no per-response CORS/cookie headers; Cache-Control
    # no-store is applied by the HTTP layer and asserted over the loopback
    # socket in test_loopback_server_smoke.
    assert "Access-Control-Allow-Origin" not in resp.extra_headers
    assert "Set-Cookie" not in resp.extra_headers
    assert "Access-Control-Allow-Origin" not in resp.body.decode()


# ---------------------------------------------------------------------------
# In-process loopback server smoke
# ---------------------------------------------------------------------------


def _raw_http(port: int, host_header: str, path: str = "/healthz",
              method: str = "GET") -> tuple[int, dict, bytes]:
    """Send a raw HTTP/1.1 request over the loopback socket with a caller-set
    Host header (urllib would override Host from the URL). Returns
    (status, headers, body) parsed from the response."""
    import socket
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        req = (f"{method} {path} HTTP/1.1\r\n"
               f"Host: {host_header}\r\n"
               "Connection: close\r\n\r\n")
        s.sendall(req.encode("utf-8"))
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return status, headers, body


def test_evil_host_header_rejected_403_before_route_or_data(tmp_path):
    """A Host header naming a non-loopback host is rejected with a generic 403
    (no-store) at the transport, before any route resolution or data access —
    even for valid routes carrying real data, and for non-GET methods."""
    info = _populate(tmp_path)
    server = web.make_server(0, tmp_path)
    port = server.server_address[1]
    import threading
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        for host in ("evil.example.com", "127.0.0.1.evil.com", "example.com:443"):
            status, headers, body = _raw_http(port, host)
            assert status == 403, host
            assert headers["cache-control"] == "no-store"
            assert json.loads(body) == {"error": "forbidden"}
        # even a valid route carrying real data must not be reachable
        status, headers, body = _raw_http(port, "evil.example.com", path="/api/v1/status")
        assert status == 403
        assert b"runs_count" not in body
        assert info["run_id"].encode() not in body
        # non-GET methods are gated the same way
        status, _headers, body = _raw_http(port, "evil.example.com", method="POST",
                                           path="/api/v1/status")
        assert status == 403
        assert json.loads(body) == {"error": "forbidden"}
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


def test_loopback_host_headers_serve_200(tmp_path):
    """Loopback Host values — plain, with a port, or bracketed IPv6 — pass the
    transport gate and serve normally."""
    info = _populate(tmp_path)
    server = web.make_server(0, tmp_path)
    port = server.server_address[1]
    import threading
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        for host in ("127.0.0.1", f"127.0.0.1:{port}", "localhost",
                     f"localhost:{port}", "[::1]", f"[::1]:{port}", "::1"):
            status, headers, body = _raw_http(port, host)
            assert status == 200, host
            assert json.loads(body) == {"status": "ok"}
        status, headers, body = _raw_http(port, "localhost", path="/api/v1/status")
        assert status == 200
        assert json.loads(body)["runs_count"] == 1
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


_SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


def test_defense_in_depth_headers_on_all_responses(tmp_path):
    """Every response — 200 JSON, 200 HTML, and the 403 evil-Host gate —
    carries X-Content-Type-Options: nosniff, X-Frame-Options: DENY and
    Referrer-Policy: no-referrer over the loopback socket."""
    info = _populate(tmp_path)
    server = web.make_server(0, tmp_path)
    port = server.server_address[1]
    import threading
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        cases = [
            ("127.0.0.1", "/healthz", 200),       # 200 JSON
            ("127.0.0.1", "/", 200),              # 200 HTML shell
            ("evil.example.com", "/healthz", 403),  # 403 evil-Host gate
        ]
        for host, path, expected_status in cases:
            status, headers, body = _raw_http(port, host, path=path)
            assert status == expected_status, (host, path)
            for name, value in _SECURITY_HEADERS.items():
                assert headers.get(name) == value, (host, path, name)
            # no-store/CORS/cookie behavior preserved on every response
            assert headers["cache-control"] == "no-store", (host, path)
            assert "access-control-allow-origin" not in headers, (host, path)
            assert "set-cookie" not in headers, (host, path)
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


def test_loopback_server_smoke(tmp_path):
    info = _populate(tmp_path)
    server = web.make_server(0, tmp_path)  # port 0 = OS-assigned
    assert server.server_address[0] == "127.0.0.1"
    port = server.server_address[1]
    import threading
    import urllib.request
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Cache-Control"] == "no-store"
            assert json.loads(resp.read()) == {"status": "ok"}
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/status", timeout=5) as resp:
            body = json.loads(resp.read())
            assert body["runs_count"] == 1
            assert body["latest_run"] == info["run_id"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/runs/deadbeef1234", timeout=5)
        assert exc.value.code == 404
        assert b"<html" in urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read()
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


# ---------------------------------------------------------------------------
# CLI seam
# ---------------------------------------------------------------------------


def test_cli_help_includes_web(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    result = runner.invoke(app, ["--help"])
    assert "web" in result.output


def test_cli_web_bad_port_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    result = runner.invoke(app, ["web", "--port", "99999"])
    assert result.exit_code == 2


def test_cli_web_non_int_port_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    result = runner.invoke(app, ["web", "--port", "abc"])
    assert result.exit_code == 2


def test_cli_web_bind_failure_exits_1(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    _populate(tmp_path)
    # occupy a loopback port, then try to bind it again
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        result = runner.invoke(app, ["web", "--port", str(port), "--no-browser"])
        assert result.exit_code == 1
    finally:
        s.close()


def test_cli_web_no_browser_prints_url_only(tmp_path, monkeypatch):
    """--no-browser serve prints only the URL to stdout and exits cleanly on
    Ctrl-C (KeyboardInterrupt), never printing secrets."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    _populate(tmp_path)
    server_box = {}

    def _fake_serve_forever(self):
        raise KeyboardInterrupt  # simulated Ctrl-C -> clean shutdown

    monkeypatch.setattr(web._WebServer, "serve_forever", _fake_serve_forever)
    result = runner.invoke(app, ["web", "--no-browser", "--port", "0"])
    assert result.exit_code == 0
    assert result.stdout.startswith("http://127.0.0.1:")
    assert "127.0.0.1" in result.stdout
    assert "token" not in result.stdout.lower()
    assert "secret" not in result.stdout.lower()
    assert "SECRET" not in result.stdout


def test_cli_web_opens_browser_unless_no_browser(tmp_path, monkeypatch):
    """Without --no-browser the browser is opened; with it, it is not."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    opened = []
    monkeypatch.setattr(web.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(web._WebServer, "serve_forever", lambda self: (_ for _ in ()).throw(KeyboardInterrupt()))

    runner.invoke(app, ["web", "--port", "0"])
    assert len(opened) == 1
    assert opened[0].startswith("http://127.0.0.1:")

    opened.clear()
    runner.invoke(app, ["web", "--port", "0", "--no-browser"])
    assert opened == []


def test_serve_bind_failure_raises_oserror(tmp_path, monkeypatch):
    """serve() propagates an OSError bind failure; the CLI maps it to exit 1
    (covered by test_cli_web_bind_failure_exits_1)."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        with pytest.raises(OSError):
            web.serve(port=port, no_browser=True, cfg_dir=tmp_path)
    finally:
        s.close()

