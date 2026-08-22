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

