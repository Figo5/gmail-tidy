"""Offline CLI tests. Every command is driven through the Typer CliRunner with
get_credentials/build_service monkeypatched, so nothing touches a network, an
OAuth browser flow, or real credentials."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gmail_tidy import cli
from gmail_tidy.audit import Candidate, RunJournal
from gmail_tidy.cli import app
from gmail_tidy.config import Actions
from tests.mock_gmail import MockGmailApi

runner = CliRunner()


def _config_text() -> str:
    return (
        "rules:\n"
        "  - id: r1\n"
        "    match: {subject_contains: [newsletter]}\n"
        "    actions:\n"
        "      add_label: [Cleanup/N]\n"
        "      archive: true\n"
    )


def _mock_net(monkeypatch, api: MockGmailApi | None = None) -> MockGmailApi:
    """Point the CLI's two module-level hooks at in-memory doubles."""
    api = api or MockGmailApi()
    monkeypatch.setattr(cli, "get_credentials", lambda cfg, require_write: object())
    monkeypatch.setattr(cli, "build_service", lambda creds: api)
    return api


def _save_run(tmp_path, cand: Candidate) -> str:
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    j.save_candidates(run_id, [cand])
    return run_id


def test_status_exit_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "config" in result.output.lower()


def test_scan_no_auth_exits_4(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 4
    assert "auth" in result.output.lower()


def test_scan_and_preview_never_write(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    _mock_net(monkeypatch, api)
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 0
    result2 = runner.invoke(app, ["preview"])
    assert result2.exit_code == 0
    # mailbox unchanged after scan + preview
    assert "Cleanup/N" not in api.store["m1"].label_ids
    assert "INBOX" in api.store["m1"].label_ids


def test_scan_noop_exits_3(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="receipt", labels={"INBOX"})  # no rule matches
    _mock_net(monkeypatch, api)
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 3


def test_missing_config_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 2
    assert "init" in result.output.lower()


def test_init_writes_template_and_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    _mock_net(monkeypatch)  # authenticated read-only
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / "config.yaml").exists()
    assert "newsletters" in (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_apply_executes_after_yes(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    _mock_net(monkeypatch, api)
    run_id = _save_run(
        tmp_path,
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
    )
    result = runner.invoke(app, ["apply", "--run", run_id, "--yes"])
    assert result.exit_code == 0
    assert "Cleanup/N" in api.store["m1"].label_ids
    assert "INBOX" not in api.store["m1"].label_ids


def test_apply_cancel_exits_5(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    _mock_net(monkeypatch, api)
    run_id = _save_run(
        tmp_path,
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
    )
    # no --yes and stdin answers "n" to the confirm
    result = runner.invoke(app, ["apply", "--run", run_id], input="n\n")
    assert result.exit_code == 5
    assert "Cleanup/N" not in api.store["m1"].label_ids


def test_undo_dry_run_by_default_no_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})  # post-apply state
    _mock_net(monkeypatch, api)
    run_id = _save_run(
        tmp_path,
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
    )
    result = runner.invoke(app, ["undo", run_id])
    assert result.exit_code == 0
    assert "Cleanup/N" in api.store["m1"].label_ids  # untouched (dry-run)
    assert "INBOX" not in api.store["m1"].label_ids


def test_undo_executes_with_yes(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})  # exactly the left-behind state
    _mock_net(monkeypatch, api)
    run_id = _save_run(
        tmp_path,
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
    )
    result = runner.invoke(app, ["undo", run_id, "--yes"])
    assert result.exit_code == 0
    assert "Cleanup/N" not in api.store["m1"].label_ids
    assert "INBOX" in api.store["m1"].label_ids


def test_undo_unknown_run_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    result = runner.invoke(app, ["undo", "does-not-exist"])
    assert result.exit_code == 2


def test_auth_status_and_revoke(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "token.json").write_text(
        '{"scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0
    assert "readonly" in result.output

    result2 = runner.invoke(app, ["auth", "revoke"])
    assert result2.exit_code == 0
    assert not (tmp_path / "token.json").exists()


def test_auth_refresh_requests_write_scopes(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    seen = {}

    def _fake_get_credentials(cfg, client_secret, require_write):
        seen["write"] = bool(require_write)
        return object()

    # auth refresh -> upgrade_write -> auth_mod.get_credentials(require_write=True)
    monkeypatch.setattr(cli.auth_mod, "get_credentials", _fake_get_credentials)
    result = runner.invoke(app, ["auth", "refresh"])
    assert result.exit_code == 0
    assert seen["write"] is True


def test_help_exit_zero_no_network(tmp_path, monkeypatch):
    """`--help` renders without touching Gmail or reading user config."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))  # status() off real config
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "scan", "preview", "apply", "undo", "status"):
        assert command in result.output


def test_module_main_runs_help_offline():
    """`python -m gmail_tidy --help` works without a package build or OAuth."""
    env = dict(os.environ)
    env.pop("GMAIL_TIDY_CONFIG", None)  # module --help must not read user config
    result = subprocess.run(
        [sys.executable, "-m", "gmail_tidy", "--help"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 0
    assert "python -m gmail_tidy" in (result.stdout + result.stderr)
