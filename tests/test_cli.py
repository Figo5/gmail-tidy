"""Offline CLI tests. Every command is driven through the Typer CliRunner with
get_credentials/build_service monkeypatched, so nothing touches a network, an
OAuth browser flow, or real credentials."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gmail_tidy import cli
from gmail_tidy import config as config_mod
from gmail_tidy.audit import Candidate, RunJournal
from gmail_tidy.checkpoint import config_fingerprint
from gmail_tidy.cli import app
from gmail_tidy.config import Actions
from gmail_tidy.errors import AuthError, ConfigError, NoWorkError, RequestError
from tests.mock_gmail import MockGmailApi, _GError

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
    assert "Cleanup/N" not in api.label_names_of("m1")
    assert "INBOX" in api.label_names_of("m1")


def test_scan_noop_exits_3(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="receipt", labels={"INBOX"})  # no rule matches
    _mock_net(monkeypatch, api)
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 3


def test_scan_noop_still_writes_run_and_checkpoint(tmp_path, monkeypatch):
    """A 0-candidate scan must still create a run file (so preview does not go
    stale) and persist a checkpoint so the next scan advances past the page."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="receipt", labels={"INBOX"})  # no rule matches
    _mock_net(monkeypatch, api)
    assert RunJournal(tmp_path / "runs").list_runs() == []
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 3  # no candidates
    assert len(RunJournal(tmp_path / "runs").list_runs()) == 1  # run still created
    assert (tmp_path / "checkpoint.json").exists()  # checkpoint persisted


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
    assert "Cleanup/N" in api.label_names_of("m1")
    assert "INBOX" not in api.label_names_of("m1")


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
    assert "Cleanup/N" not in api.label_names_of("m1")


def test_apply_unknown_run_exits_2_no_traceback(tmp_path, monkeypatch):
    """A --run id with no journal file must exit 2 (config error) with the clean
    FileNotFoundError message — never Click's raw traceback handler."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    _mock_net(monkeypatch)  # never reached: load_candidates raises first
    result = runner.invoke(app, ["apply", "--run", "000000000000", "--yes"])
    assert result.exit_code == 2
    assert "not found" in result.output.lower()  # the clean FileNotFoundError text
    assert "Traceback" not in result.output
    # Typer.Exit(2) surfaces as SystemExit(2), proving the command body caught
    # FileNotFoundError instead of leaking it to Click's default handler.
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == 2


def test_apply_no_runs_exits_3(tmp_path, monkeypatch):
    """No --run given and no runs exist: the separate no-op branch still exits 3
    (EXIT_NOOP) — untouched by the unknown-run FileNotFoundError fix."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    _mock_net(monkeypatch)
    result = runner.invoke(app, ["apply", "--yes"])
    assert result.exit_code == 3
    assert "no run found" in result.output.lower()


# --- apply prints the proposed diff before confirmation (Task 33) -----------
# Interactive apply must render the same id/rule/actions table as preview
# (via render.action_text) BEFORE asking for confirmation — for BOTH the
# prompt path and --yes — so the user always sees exactly what will change.
# Output must stay aggregate/privacy-safe: only message ids, rule ids, and
# action summaries; never sender/subject/body/size/content.


def test_apply_yes_shows_proposed_actions_table_before_executing(tmp_path, monkeypatch):
    """--yes: the id/rule/actions table is printed before apply_run writes."""
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
    # the proposed diff is rendered (id/rule/actions) before the write happens
    assert "proposed" in result.output   # apply table title (word never split by wrap)
    assert "(apply)" in result.output    # distinct from preview's "(dry-run)" title
    assert "m1" in result.output         # message id (the table's id column)
    assert "r1" in result.output         # rule id
    assert "+Cleanup/N, archive" in result.output  # action summary via action_text
    # the table precedes the "will be modified" line
    assert result.output.index("+Cleanup/N, archive") < result.output.index("will be modified")
    # privacy: never sender/subject/body/size/content from the mailbox
    assert "newsletter" not in result.output            # subject
    assert "sender@example.com" not in result.output    # sender
    assert "size" not in result.output.lower()
    assert "Cleanup/N" in api.label_names_of("m1")      # it really applied
    assert "INBOX" not in api.label_names_of("m1")


def test_apply_prompt_shows_proposed_actions_table_before_confirm(tmp_path, monkeypatch):
    """The prompt path renders the diff BEFORE 'Proceed with apply?' appears."""
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
    result = runner.invoke(app, ["apply", "--run", run_id], input="n\n")
    assert result.exit_code == 5  # decline cancels, no write
    assert "proposed" in result.output   # apply table title (word never split by wrap)
    assert "(apply)" in result.output    # distinct from preview's "(dry-run)" title
    assert "+Cleanup/N, archive" in result.output
    assert "Proceed with apply?" in result.output
    # the diff is printed BEFORE the confirmation prompt
    assert result.output.index("+Cleanup/N, archive") < result.output.index("Proceed with apply?")
    assert "Cleanup/N" not in api.label_names_of("m1")  # nothing written


def test_apply_diff_privacy_no_content(tmp_path, monkeypatch):
    """Apply's diff prints only id/rule/action summaries — never content."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="SECRET-SUBJECT", from_hdr="attacker@example.com",
                    size_kb=123.0, labels={"INBOX"})
    _mock_net(monkeypatch, api)
    run_id = _save_run(
        tmp_path,
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
    )
    result = runner.invoke(app, ["apply", "--run", run_id, "--yes"])
    assert result.exit_code == 0
    assert "m1" in result.output       # id column is allowed
    assert "r1" in result.output       # rule column is allowed
    assert "+Cleanup/N" in result.output  # action summary is allowed
    # forbidden: sender, subject, body, size, or raw content
    assert "SECRET-SUBJECT" not in result.output
    assert "attacker@example.com" not in result.output
    assert "123.0" not in result.output
    assert "body" not in result.output.lower()


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
    assert "Cleanup/N" in api.label_names_of("m1")  # untouched (dry-run)
    assert "INBOX" not in api.label_names_of("m1")


def test_undo_executes_with_apply_yes(tmp_path, monkeypatch):
    """--apply --yes writes immediately with no prompt (for automation)."""
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
    result = runner.invoke(app, ["undo", run_id, "--apply", "--yes"])
    assert result.exit_code == 0
    assert "Cleanup/N" not in api.label_names_of("m1")
    assert "INBOX" in api.label_names_of("m1")


def test_undo_dry_run_flag_no_writes(tmp_path, monkeypatch):
    """Explicit --dry-run is identical to no-flags: preview only, exit 0."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    _mock_net(monkeypatch, api)
    run_id = _save_run(
        tmp_path,
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
    )
    result = runner.invoke(app, ["undo", run_id, "--dry-run"])
    assert result.exit_code == 0
    assert "Cleanup/N" in api.label_names_of("m1")  # untouched (dry-run)
    assert "INBOX" not in api.label_names_of("m1")


def test_undo_yes_without_apply_is_usage_error(tmp_path, monkeypatch):
    """--yes without --apply is a nonsensical combination → usage error, no write."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    _mock_net(monkeypatch, api)
    run_id = _save_run(
        tmp_path,
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
    )
    result = runner.invoke(app, ["undo", run_id, "--yes"])
    assert result.exit_code == 2
    assert "--yes" in result.output and "--apply" in result.output
    assert "Cleanup/N" in api.label_names_of("m1")  # no write occurred
    assert "INBOX" not in api.label_names_of("m1")


def test_undo_apply_prompt_decline_cancels(tmp_path, monkeypatch):
    """--apply alone prompts; declining prints 'cancelled.' and exits 5, no write."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    _mock_net(monkeypatch, api)
    run_id = _save_run(
        tmp_path,
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
    )
    result = runner.invoke(app, ["undo", run_id, "--apply"], input="n\n")
    assert result.exit_code == 5
    assert "cancelled." in result.output
    assert "Cleanup/N" in api.label_names_of("m1")  # no write occurred
    assert "INBOX" not in api.label_names_of("m1")


def test_undo_apply_prompt_accept_writes(tmp_path, monkeypatch):
    """--apply alone; accepting the prompt writes and exits 0."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    _mock_net(monkeypatch, api)
    run_id = _save_run(
        tmp_path,
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
    )
    result = runner.invoke(app, ["undo", run_id, "--apply"], input="y\n")
    assert result.exit_code == 0
    assert "Cleanup/N" not in api.label_names_of("m1")  # write happened
    assert "INBOX" in api.label_names_of("m1")


def test_undo_apply_yes_never_prompts(tmp_path, monkeypatch):
    """--apply --yes must NEVER touch stdin (automation safety)."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    _mock_net(monkeypatch, api)
    run_id = _save_run(
        tmp_path,
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
    )

    def _fail_if_confirmed(prompt):
        raise AssertionError("--apply --yes must never call typer.confirm (stdin read)")

    monkeypatch.setattr(cli.typer, "confirm", _fail_if_confirmed)
    # No `input=` provided at all — if code tried to read stdin this would hang/fail.
    result = runner.invoke(app, ["undo", run_id, "--apply", "--yes"])
    assert result.exit_code == 0
    assert "Cleanup/N" not in api.label_names_of("m1")  # write happened
    assert "INBOX" in api.label_names_of("m1")


def test_undo_dry_run_wins_over_apply(tmp_path, monkeypatch):
    """--dry-run --apply together: dry-run wins, preview only, no write."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    _mock_net(monkeypatch, api)
    run_id = _save_run(
        tmp_path,
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
    )
    result = runner.invoke(app, ["undo", run_id, "--dry-run", "--apply"])
    assert result.exit_code == 0
    assert "Cleanup/N" in api.label_names_of("m1")  # untouched (preview won)
    assert "INBOX" not in api.label_names_of("m1")


def test_undo_apply_yes_skips_user_changed_message(tmp_path, monkeypatch):
    """CLI-level: exact-state guard still works through the --apply --yes path."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N", "B"})  # user added B since apply
    _mock_net(monkeypatch, api)
    run_id = _save_run(
        tmp_path,
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
    )
    result = runner.invoke(app, ["undo", run_id, "--apply", "--yes"])
    assert result.exit_code == 0
    # user label B untouched; INBOX NOT re-added (message was user-changed)
    assert "B" in api.label_names_of("m1")
    assert "INBOX" not in api.label_names_of("m1")
    assert "Cleanup/N" in api.label_names_of("m1")


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


def _config_text_two_rules() -> str:
    return (
        "rules:\n"
        "  - id: r1\n"
        "    match: {subject_contains: [alpha]}\n"
        "    actions:\n"
        "      add_label: [Cleanup/A]\n"
        "      archive: true\n"
        "  - id: r2\n"
        "    match: {subject_contains: [beta]}\n"
        "    actions:\n"
        "      add_label: [Cleanup/B]\n"
        "      archive: true\n"
    )


def _load_two_rule_config(path: Path) -> config_mod.Config:
    """Load the two-rule config file so its FULL-config fingerprint can be
    computed (the CLI's fingerprint-invariant these regression tests assert on)."""
    return config_mod.load_config(path)


# --- scan --all ------------------------------------------------------------


def test_scan_all_and_limit_usage_error(tmp_path, monkeypatch):
    """--all combined with --limit is a usage error (exit 2) mentioning both flags."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    _mock_net(monkeypatch, api)
    result = runner.invoke(app, ["scan", "--all", "--limit", "5"])
    assert result.exit_code == 2
    assert "--all" in result.output and "--limit" in result.output


def test_scan_all_runs_and_writes_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    api.add_message("m2", subject="newsletter", labels={"INBOX"})
    api.add_message("m3", subject="newsletter", labels={"INBOX"})
    _mock_net(monkeypatch, api)
    result = runner.invoke(app, ["scan", "--all"])
    assert result.exit_code == 0
    assert "3 candidate" in result.output
    # the mock mailbox's label state is byte-for-byte unchanged after --all
    assert "Cleanup/N" not in api.label_names_of("m1")
    assert "Cleanup/N" not in api.label_names_of("m2")
    assert "Cleanup/N" not in api.label_names_of("m3")
    assert api.label_names_of("m1") == {"INBOX"}
    assert api.label_names_of("m2") == {"INBOX"}
    assert api.label_names_of("m3") == {"INBOX"}
    # checkpoint persisted with exhausted=True for the fully-consumed rule
    import json
    data = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert data["rules"]["r1"]["exhausted"] is True
    assert data["rules"]["r1"]["page_token"] is None


def test_scan_all_output_has_no_message_ids(tmp_path, monkeypatch):
    """--all progress lines must never leak message ids; only rule ids + counts."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    # distinctive id that would trip any accidental id-in-output bug
    api.add_message("SECRET-MSG-ID-1", subject="newsletter", labels={"INBOX"})
    api.add_message("SECRET-MSG-ID-2", subject="newsletter", labels={"INBOX"})
    _mock_net(monkeypatch, api)
    result = runner.invoke(app, ["scan", "--all"])
    assert result.exit_code == 0
    assert "SECRET-MSG-ID" not in result.output
    # but the rule id and candidate count DO appear
    assert "r1" in result.output
    assert "2 candidate" in result.output


def test_scan_all_with_rules_subset(tmp_path, monkeypatch):
    """--all --rules r1 exhausts only r1. On a FRESH scan (no prior checkpoint),
    r2 has no state to carry forward, so the checkpoint contains only r1's entry.
    The r2-preservation path (r2 previously scanned) is covered by
    test_scan_all_then_rules_subset_preserves_r2_checkpoint below."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text_two_rules(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="alpha", labels={"INBOX"})
    api.add_message("m2", subject="beta", labels={"INBOX"})
    _mock_net(monkeypatch, api)
    result = runner.invoke(app, ["scan", "--all", "--rules", "r1"])
    assert result.exit_code == 0
    assert "1 candidate" in result.output
    import json
    data = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert set(data["rules"].keys()) == {"r1"}  # only r1 scanned/exhausted (fresh scan)
    assert data["rules"]["r1"]["exhausted"] is True


def test_scan_all_then_rules_subset_preserves_r2_checkpoint(tmp_path, monkeypatch):
    """Regression: an unscoped --all scan produces checkpoint entries for r1+r2;
    a later --rules r1 scan must NOT drop r2's entry or change the fingerprint.
    r1's entry updates (a non--all scoped scan records exhausted=False), r2's
    entry is preserved byte-for-byte, and the stored fingerprint stays the FULL
    config's hash so a later full scan still resumes correctly."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text_two_rules(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("a1", subject="alpha", labels={"INBOX"})
    api.add_message("a2", subject="alpha", labels={"INBOX"})
    api.add_message("b1", subject="beta", labels={"INBOX"})
    api.add_message("b2", subject="beta", labels={"INBOX"})
    _mock_net(monkeypatch, api)

    # full --all scan: both rules exhausted with clean (None) resume points
    result = runner.invoke(app, ["scan", "--all"])
    assert result.exit_code == 0
    assert "4 candidate" in result.output
    data1 = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert set(data1["rules"].keys()) == {"r1", "r2"}
    assert data1["rules"]["r1"]["exhausted"] is True
    assert data1["rules"]["r2"]["exhausted"] is True
    fp = config_fingerprint(_load_two_rule_config(tmp_path / "config.yaml"))

    # scoped scan of r1 only: r1 re-scanned (exhausted=False again — a plain
    # scan never records exhaustion), r2's prior entry must survive unchanged.
    result2 = runner.invoke(app, ["scan", "--rules", "r1"])
    assert result2.exit_code == 0
    data2 = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert set(data2["rules"].keys()) == {"r1", "r2"}   # r2 NOT dropped
    assert data2["rules"]["r1"]["exhausted"] is False   # r1 updated
    assert data2["rules"]["r2"] == data1["rules"]["r2"]  # r2 unchanged
    assert data2["config_fingerprint"] == fp             # full-config hash kept
    assert data2["config_fingerprint"] == data1["config_fingerprint"]


def test_scan_rules_subset_resumes_from_prior_page_token(tmp_path, monkeypatch):
    """Regression: a --rules-scoped scan must resume the selected rule from ITS
    OWN prior page_token (not restart at page 1). A --limit 3 scan stops
    mid-mailbox on page 2, persisting r1's resume token "2"; a second scoped
    --rules r1 scan continues from there and finds only the 2 remaining
    messages (a3,a4) — restarting at page 1 would have re-found a1,a2,a3 (3)."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text_two_rules(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("a1", subject="alpha", labels={"INBOX"})
    api.add_message("a2", subject="alpha", labels={"INBOX"})
    api.add_message("a3", subject="alpha", labels={"INBOX"})
    api.add_message("a4", subject="alpha", labels={"INBOX"})
    _mock_net(monkeypatch, api)

    # scan 1 (unscoped): r1 first, limit hits on page 2 -> r1 resume token "2"
    result = runner.invoke(app, ["scan", "--limit", "3"])
    assert result.exit_code == 0
    assert "3 candidate" in result.output
    data1 = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert data1["rules"]["r1"]["page_token"] == "2"  # resume mid-mailbox
    fp = config_fingerprint(_load_two_rule_config(tmp_path / "config.yaml"))

    # scan 2 (scoped): resumes at token "2" -> a3,a4 (2 candidates), not a1..a3
    result2 = runner.invoke(app, ["scan", "--rules", "r1", "--limit", "3"])
    assert result2.exit_code == 0
    assert "2 candidate" in result2.output
    data2 = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    # r1 consumed its remaining page; fingerprint stays the full-config hash
    assert data2["rules"]["r1"]["page_token"] is None
    assert data2["config_fingerprint"] == fp


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


# --- summary ---------------------------------------------------------------


def _save_run_with_stats(tmp_path, candidates, stats=None) -> str:
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    j.save_candidates(run_id, candidates)
    if stats is not None:
        j.save_stats(run_id, stats)
    return run_id


def test_summary_mixed_rules_actions_labels(tmp_path, monkeypatch):
    """Candidates spanning 2+ rules, add+remove labels, some archived some not."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _save_run_with_stats(
        tmp_path,
        [
            Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                      actions=Actions(add_label=["Cleanup/A"], remove_label=["Promo"],
                                      archive=True), in_inbox=True),
            Candidate(message_id="m2", thread_id="t2", rule_id="r1",
                      actions=Actions(add_label=["Cleanup/A"], archive=False), in_inbox=True),
            Candidate(message_id="m3", thread_id="t3", rule_id="r2",
                      actions=Actions(add_label=["Cleanup/B"], archive=False), in_inbox=False),
        ],
        stats={"evaluated": 10, "excluded": 4, "noop": 2, "candidates": 3},
    )
    result = runner.invoke(app, ["summary", "--run", run_id])
    assert result.exit_code == 0
    out = result.output
    # totals
    assert "3" in out  # total candidates
    # by rule
    assert "r1" in out and "r2" in out
    # by action: 2 add_label ops, 1 remove_label op, 1 archived
    # labels added: Cleanup/A (x2), Cleanup/B (x1); removed: Promo (x1)
    assert "Cleanup/A" in out
    assert "Cleanup/B" in out
    assert "Promo" in out
    # archive breakdown: labels-only count == 2, archive-action count == 1
    # inbox reduction == 1 (only m1 is both in_inbox and archive)
    # scan stats present
    assert "evaluated" in out and "excluded" in out


def test_summary_zero_candidate_run_exits_zero(tmp_path, monkeypatch):
    """A 0-candidate run is a legitimate already-created run: exit 0, not an error."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _save_run_with_stats(tmp_path, [],
                                  stats={"evaluated": 0, "excluded": 5, "noop": 2, "candidates": 0})
    result = runner.invoke(app, ["summary", "--run", run_id])
    assert result.exit_code == 0
    assert "0" in result.output


def test_summary_no_runs_exits_3(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    result = runner.invoke(app, ["summary"])
    assert result.exit_code == 3
    assert "no run found" in result.output


def test_summary_old_run_without_stats_graceful(tmp_path, monkeypatch):
    """Old run saved before stats existed: no crash, 'not recorded' message, exit 0."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _save_run_with_stats(
        tmp_path,
        [Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                   actions=Actions(add_label=["Cleanup/A"], archive=True), in_inbox=True)],
        stats=None,  # no stats file written (simulates pre-feature run)
    )
    result = runner.invoke(app, ["summary", "--run", run_id])
    assert result.exit_code == 0
    assert "not recorded" in result.output.lower()


def test_summary_checkpoint_exhaustion_state(tmp_path, monkeypatch):
    """One exhausted rule + one in-progress rule both shown correctly."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _save_run_with_stats(
        tmp_path,
        [Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                   actions=Actions(add_label=["Cleanup/A"], archive=True), in_inbox=True)],
        stats={"evaluated": 1, "excluded": 0, "noop": 0, "candidates": 1},
    )
    (tmp_path / "checkpoint.json").write_text(
        json.dumps({
            "config_fingerprint": "fp",
            "rules": {
                "r1": {"page_token": None, "exhausted": True},
                "r2": {"page_token": "abc123", "exhausted": False},
            },
        }),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["summary", "--run", run_id])
    assert result.exit_code == 0
    out = result.output
    assert "exhausted" in out.lower()
    # r1 exhausted, r2 in-progress (has a page token)
    assert "r1" in out and "r2" in out


def test_summary_checkpoint_missing_graceful(tmp_path, monkeypatch):
    """No checkpoint.json present: print 'no checkpoint yet' rather than crash."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _save_run_with_stats(
        tmp_path,
        [Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                   actions=Actions(add_label=["Cleanup/A"], archive=True), in_inbox=True)],
        stats={"evaluated": 1, "excluded": 0, "noop": 0, "candidates": 1},
    )
    result = runner.invoke(app, ["summary", "--run", run_id])
    assert result.exit_code == 0
    assert "no checkpoint" in result.output.lower()


def test_summary_inbox_reduction_calculation(tmp_path, monkeypatch):
    """True/True -> counted; True/False and False/True -> not counted. Reduction == 1."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _save_run_with_stats(
        tmp_path,
        [
            Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                      actions=Actions(archive=True), in_inbox=True),   # counted
            Candidate(message_id="m2", thread_id="t2", rule_id="r1",
                      actions=Actions(archive=False), in_inbox=True),  # not archived
            Candidate(message_id="m3", thread_id="t3", rule_id="r1",
                      actions=Actions(archive=True), in_inbox=False),  # not in inbox
        ],
        stats={"evaluated": 3, "excluded": 0, "noop": 0, "candidates": 3},
    )
    result = runner.invoke(app, ["summary", "--run", run_id])
    assert result.exit_code == 0
    out = result.output
    # assert exactly the inbox-reduction line equals 1 (not 3)
    # cli.py prints f"  inbox reduction  : {inbox_reduction}" (2 spaces each side of the colon)
    assert "  inbox reduction  : 1" in out


def test_summary_output_has_no_message_ids(tmp_path, monkeypatch):
    """Distinctive fake ids must never leak into summary output."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _save_run_with_stats(
        tmp_path,
        [Candidate(message_id="SECRET-MSG-ID-1", thread_id="SECRET-THREAD-ID",
                   rule_id="r1", actions=Actions(add_label=["Cleanup/A"], archive=True),
                   in_inbox=True)],
        stats={"evaluated": 1, "excluded": 0, "noop": 0, "candidates": 1},
    )
    result = runner.invoke(app, ["summary", "--run", run_id])
    assert result.exit_code == 0
    assert "SECRET-MSG-ID" not in result.output
    assert "SECRET-THREAD-ID" not in result.output


def test_summary_makes_zero_gmail_calls(tmp_path, monkeypatch):
    """summary must never touch credentials/service — prove with an assertion raise."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _save_run_with_stats(
        tmp_path,
        [Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                   actions=Actions(add_label=["Cleanup/A"], archive=True), in_inbox=True)],
        stats={"evaluated": 1, "excluded": 0, "noop": 0, "candidates": 1},
    )

    def _boom(*a, **k):
        raise AssertionError("summary must not call get_credentials/build_service")

    monkeypatch.setattr(cli, "get_credentials", _boom)
    monkeypatch.setattr(cli, "build_service", _boom)
    result = runner.invoke(app, ["summary", "--run", run_id])
    assert result.exit_code == 0


def test_summary_unknown_run_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    result = runner.invoke(app, ["summary", "--run", "does-not-exist"])
    assert result.exit_code == 2


def test_summary_defaults_to_latest_run(tmp_path, monkeypatch):
    """No --run: summary reports the LATEST run (by mtime), not an older one."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    old_id = _save_run_with_stats(
        tmp_path,
        [Candidate(message_id="m1", thread_id="t1", rule_id="old-rule",
                   actions=Actions(add_label=["Cleanup/Old"], archive=True), in_inbox=True)],
        stats={"evaluated": 1, "excluded": 0, "noop": 0, "candidates": 1},
    )
    new_id = _save_run_with_stats(
        tmp_path,
        [Candidate(message_id="m2", thread_id="t2", rule_id="new-rule",
                   actions=Actions(add_label=["Cleanup/New"], archive=True), in_inbox=True)],
        stats={"evaluated": 1, "excluded": 0, "noop": 0, "candidates": 1},
    )
    # Force deterministic ordering: two writes within one clock tick get equal
    # mtimes on Windows, so pin the OLD run's file to an explicitly earlier time.
    os.utime(tmp_path / "runs" / f"{old_id}.json", (1000000, 1000000))
    os.utime(tmp_path / "runs" / f"{new_id}.json", (2000000, 2000000))
    result = runner.invoke(app, ["summary"])
    assert result.exit_code == 0
    assert "new-rule" in result.output
    assert "old-rule" not in result.output


def test_summary_with_stats_section(tmp_path, monkeypatch):
    """Scan stats section shows evaluated/excluded/noop/candidates when present."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _save_run_with_stats(
        tmp_path,
        [Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                   actions=Actions(add_label=["Cleanup/A"], archive=True), in_inbox=True)],
        stats={"evaluated": 7, "excluded": 3, "noop": 1, "candidates": 1},
    )
    result = runner.invoke(app, ["summary", "--run", run_id])
    assert result.exit_code == 0
    assert "7" in result.output
    assert "3" in result.output
    assert "1" in result.output


# --- preview: --compact / --explain / --json --------------------------------


def _preview_run(tmp_path, candidates):
    """Save a run (no config.yaml needed) and return its id."""
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    j.save_candidates(run_id, candidates)
    return run_id


def test_preview_compact_groups_by_rule_no_config(tmp_path, monkeypatch):
    """--compact reads only the run journal; no config.yaml, token, or Gmail."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _preview_run(
        tmp_path,
        [
            Candidate(message_id="SECRET-MSG-ID-1", thread_id="SECRET-THREAD-ID", rule_id="r1",
                      actions=Actions(add_label=["Cleanup/N"], archive=True), in_inbox=True),
            Candidate(message_id="SECRET-MSG-ID-2", thread_id="SECRET-THREAD-ID", rule_id="r1",
                      actions=Actions(add_label=["Cleanup/N"], archive=True), in_inbox=True),
            Candidate(message_id="SECRET-MSG-ID-3", thread_id="SECRET-THREAD-ID", rule_id="r2",
                      actions=Actions(archive=True), in_inbox=True),
        ],
    )
    result = runner.invoke(app, ["preview", "--run", run_id, "--compact"])
    assert result.exit_code == 0
    out = result.output
    assert "r1: 2 candidate(s)" in out
    assert "r2: 1 candidate(s)" in out
    assert "+Cleanup/N, archive (x2)" in out
    # privacy: no message or thread ids
    assert "SECRET-MSG-ID" not in out
    assert "SECRET-THREAD-ID" not in out


def test_preview_plain_works_without_config(tmp_path, monkeypatch):
    """The default preview must keep working with no config.yaml present."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _preview_run(
        tmp_path,
        [Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                   actions=Actions(add_label=["Cleanup/N"], archive=True), in_inbox=True)],
    )
    result = runner.invoke(app, ["preview", "--run", run_id])
    assert result.exit_code == 0
    assert "Cleanup/N" in result.output and "archive" in result.output


def test_preview_compact_zero_gmail_calls(tmp_path, monkeypatch):
    """--compact must never touch credentials/service — prove with assertion raise."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _preview_run(
        tmp_path,
        [Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                   actions=Actions(add_label=["Cleanup/N"], archive=True), in_inbox=True)],
    )

    def _boom(*a, **k):
        raise AssertionError("preview --compact must not call get_credentials/build_service")

    monkeypatch.setattr(cli, "get_credentials", _boom)
    monkeypatch.setattr(cli, "build_service", _boom)
    result = runner.invoke(app, ["preview", "--run", run_id, "--compact"])
    assert result.exit_code == 0


def test_preview_json_serializes_run_file_fields_only(tmp_path, monkeypatch):
    """--json emits exactly the whitelisted run-file fields and a run key."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _preview_run(
        tmp_path,
        [Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                   actions=Actions(add_label=["Cleanup/N"], remove_label=["Promo"], archive=True),
                   before_labels={"INBOX", "Promo"}, in_inbox=True)],
    )
    result = runner.invoke(app, ["preview", "--run", run_id, "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["run"] == run_id
    cand = data["candidates"][0]
    assert set(cand) == {"message_id", "thread_id", "rule_id", "actions",
                         "before_labels", "in_inbox"}
    assert set(cand["actions"]) == {"add_label", "remove_label", "archive"}
    # no new privacy-sensitive fields invented by JSON
    assert "sender" not in cand and "subject" not in cand and "body" not in cand
    assert cand["before_labels"] == ["INBOX", "Promo"]  # sorted by the journal


def test_preview_json_conflicts_with_compact(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _preview_run(
        tmp_path,
        [Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                   actions=Actions(add_label=["Cleanup/N"], archive=True), in_inbox=True)],
    )
    result = runner.invoke(app, ["preview", "--run", run_id, "--json", "--compact"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output.lower()


def test_preview_explain_requires_config(tmp_path, monkeypatch):
    """--explain needs config.yaml: missing config exits 2, Gmail never touched."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))

    def _boom(*a, **k):
        raise AssertionError("preview --explain must not call get_credentials/build_service")

    monkeypatch.setattr(cli, "get_credentials", _boom)
    monkeypatch.setattr(cli, "build_service", _boom)
    result = runner.invoke(app, ["preview", "--explain"])
    assert result.exit_code == 2
    assert "no config" in result.output.lower()
    assert "init" in result.output


def test_preview_explain_shows_criteria_only(tmp_path, monkeypatch):
    """--explain prints match criteria from config.yaml, never actions or data."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "rules:\n"
        "  - id: r1\n"
        "    match: {subject_contains: [newsletter], older_than_days: 30, unread: true}\n"
        "    actions:\n"
        "      add_label: [Cleanup/N]\n"
        "      archive: true\n"
        "  - id: r2\n"
        "    match: {from_contains: [news@example.com]}\n"
        "    actions:\n"
        "      add_label: [Cleanup/M]\n",
        encoding="utf-8",
    )

    def _boom(*a, **k):
        raise AssertionError("preview --explain must not call get_credentials/build_service")

    monkeypatch.setattr(cli, "get_credentials", _boom)
    monkeypatch.setattr(cli, "build_service", _boom)
    result = runner.invoke(app, ["preview", "--explain"])
    assert result.exit_code == 0
    out = result.output
    assert "r1:" in out
    assert "subject_contains: newsletter" in out
    assert "older_than_days: 30" in out
    assert "unread: true" in out
    assert "r2:" in out
    assert "from_contains: news@example.com" in out
    # actions are never part of explain
    assert "Cleanup/N" not in out
    assert "archive" not in out
    # no run data involved
    assert "candidate" not in out.lower()


def test_preview_explain_conflicts(tmp_path, monkeypatch):
    """--explain combined with --compact or --json is a usage error (exit 2)."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    for combo in (["--explain", "--compact"], ["--explain", "--json"]):
        result = runner.invoke(app, ["preview", *combo])
        assert result.exit_code == 2
        assert "--explain" in result.output


def test_preview_unknown_run_no_config_exits_2(tmp_path, monkeypatch):
    """No config + explicit unknown run still surfaces the missing run."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    result = runner.invoke(app, ["preview", "--run", "does-not-exist", "--compact"])
    assert result.exit_code == 2
    assert "not found" in result.output.lower()


def test_preview_explain_empty_rules_config(tmp_path, monkeypatch):
    """A config with no rules: explain prints the no-rules notice, exit 0."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text("rules: []\n", encoding="utf-8")
    result = runner.invoke(app, ["preview", "--explain"])
    assert result.exit_code == 0
    assert "no rules" in result.output.lower()


# --- persistent Gmail failure -> RequestError -> clean exit 1 ----------------
# gmail_client._execute retries 500/503 up to MAX_RETRIES then raises
# RequestError. Before Task 28 that error escaped the command bodies' narrow
# catch tuples and Click printed a raw traceback. These tests prove the four
# commands catch it, print the clean red message, and exit 1 with no traceback.


def _persistent_500(api: MockGmailApi, method: str) -> None:
    """Make `method` fail with HTTP 500 on every call (retries exhausted)."""
    api._handlers[method] = lambda **kw: (_ for _ in ()).throw(_GError(500, "boom"))


def _assert_clean_request_error(result, expected_text: str = "Gmail request failed"):
    assert result.exit_code == 1
    # The RequestError itself must never reach Click's default handler (that
    # would leak a raw traceback). Typer.Exit(1) surfaces as SystemExit(1) —
    # the same shape the existing ConfigError path produces — proving the
    # command body caught the error and converted it to the exit path.
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == 1
    assert "Traceback" not in result.output
    assert expected_text in result.output  # the clean error message


def test_scan_persistent_gmail_failure_exits_1_no_traceback(tmp_path, monkeypatch):
    """labels.list 500 -> fetch_label_index -> RequestError -> clean exit 1."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    _mock_net(monkeypatch, api)
    _persistent_500(api, "labels.list")
    monkeypatch.setattr("time.sleep", lambda s: None)
    result = runner.invoke(app, ["scan"])
    _assert_clean_request_error(result)


def test_run_persistent_gmail_failure_exits_1_no_traceback(tmp_path, monkeypatch):
    """run scan path: labels.list 500 -> RequestError -> clean exit 1 (headless)."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    (tmp_path / "token.json").write_text(
        json.dumps({
            "token": "fake-token",
            "refresh_token": "fake-refresh",
            "client_id": "x",
            "client_secret": "x",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": [
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/gmail.labels",
            ],
        }),
        encoding="utf-8",
    )
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    monkeypatch.setattr(cli, "build_service", lambda creds: api)
    _persistent_500(api, "labels.list")
    monkeypatch.setattr("time.sleep", lambda s: None)
    result = runner.invoke(app, ["run"])
    _assert_clean_request_error(result)


def test_apply_fetch_label_index_failure_exits_1_no_traceback(tmp_path, monkeypatch):
    """apply's labels.list (fetch_label_index outside per-candidate try) -> exit 1."""
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
    _persistent_500(api, "labels.list")
    monkeypatch.setattr("time.sleep", lambda s: None)
    result = runner.invoke(app, ["apply", "--run", run_id, "--yes"])
    _assert_clean_request_error(result)


def test_undo_fetch_label_index_failure_exits_1_no_traceback(tmp_path, monkeypatch):
    """undo's labels fetch (execute_undo, before per-message guard) -> exit 1."""
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
    _persistent_500(api, "labels.list")
    monkeypatch.setattr("time.sleep", lambda s: None)
    result = runner.invoke(app, ["undo", run_id, "--apply", "--yes"])
    _assert_clean_request_error(result)


def test_exit_code_mapping_unchanged_for_existing_errors():
    """Regression guard: _exit_for mappings for ConfigError/AuthError/NoWorkError
    are unchanged (2/4/3) and RequestError still maps to EXIT_RUNTIME (1)."""
    assert cli._exit_for(ConfigError("c")) == 2
    assert cli._exit_for(AuthError("a")) == 4
    assert cli._exit_for(NoWorkError("n")) == 3
    assert cli._exit_for(RequestError("r")) == 1


# --- mid-run AuthError (403) -> clean exit 4 --------------------------------
# apply_run re-raises AuthError (Task 30) so a 403 mid-run escapes the command
# bodies and reaches the auth exit path: exit 4 + the re-authenticate message,
# never a misleading EXIT_PARTIAL and never a raw traceback.


def _assert_clean_auth_exit(result,
                            expected_text: str = "run `gmail-tidy auth` to re-authenticate"):
    assert result.exit_code == 4
    # Typer.Exit(4) surfaces as SystemExit(4) — the same shape the RequestError
    # path produces — proving the command body caught AuthError and converted
    # it to the exit path rather than leaking a raw traceback.
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == 4
    assert "Traceback" not in result.output
    assert expected_text in result.output  # the clean re-authenticate message


def _inject_403_on_get_call(api: MockGmailApi, fail_at_call: int) -> None:
    """Make the n-th get() call raise HTTP 403 (mid-run token revocation)."""
    calls = {"n": 0}
    orig = api._handlers["get"]

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == fail_at_call:
            raise _GError(403, "denied")
        return orig(**kw)

    api._handlers["get"] = flaky


def test_apply_midrun_403_exits_4_no_traceback(tmp_path, monkeypatch):
    """apply: a 403 on the second candidate's get_meta (m1 already applied)
    must propagate to the auth exit path — exit 4, re-auth message, no partial."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    api.add_message("m2", subject="newsletter", labels={"INBOX"})
    _mock_net(monkeypatch, api)
    # both candidates live in ONE run so apply processes m1 then m2
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    j.save_candidates(run_id, [
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
        Candidate(message_id="m2", thread_id="t2", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
    ])
    # apply_run re-verifies each candidate via get_meta: m1 (call 1) succeeds,
    # m2 (call 2) hits the 403.
    _inject_403_on_get_call(api, fail_at_call=2)
    result = runner.invoke(app, ["apply", "--run", run_id, "--yes"])
    _assert_clean_auth_exit(result)
    # m1 was applied before the 403 hit
    assert "Cleanup/N" in api.label_names_of("m1")


def test_apply_midrun_403_batch_modify_exits_4(tmp_path, monkeypatch):
    """A 403 on batch_modify mid-apply must also propagate to exit 4."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    api.add_message("m2", subject="newsletter", labels={"INBOX"})
    _mock_net(monkeypatch, api)
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    j.save_candidates(run_id, [
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
        Candidate(message_id="m2", thread_id="t2", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True),
    ])
    calls = {"n": 0}
    orig = api._handlers["batchModify"]

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 2:  # m2's write 403s after m1 succeeded
            raise _GError(403, "denied")
        return orig(**kw)

    api._handlers["batchModify"] = flaky
    result = runner.invoke(app, ["apply", "--run", run_id, "--yes"])
    _assert_clean_auth_exit(result)
    assert "Cleanup/N" in api.label_names_of("m1")


def test_run_midrun_403_exits_4_no_traceback(tmp_path, monkeypatch):
    """run (headless): a 403 mid-apply after scan succeeds must propagate out
    of run_cycle's apply_run and exit 4 with the re-authenticate message."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    (tmp_path / "token.json").write_text(
        json.dumps({
            "token": "fake-token",
            "refresh_token": "fake-refresh",
            "client_id": "x",
            "client_secret": "x",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": [
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/gmail.labels",
            ],
        }),
        encoding="utf-8",
    )
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    api.add_message("m2", subject="newsletter", labels={"INBOX"})
    monkeypatch.setattr(cli, "build_service", lambda creds: api)
    # scan re-verifies m1 (get 1), m2 (get 2); apply re-verifies m1 (get 3),
    # m2 (get 4) -> 403 hits the apply re-verify of m2.
    _inject_403_on_get_call(api, fail_at_call=4)
    result = runner.invoke(app, ["run"])
    _assert_clean_auth_exit(result)
    # m1 was applied before the 403 hit
    assert "Cleanup/N" in api.label_names_of("m1")


# --- surface already-recorded apply failures in CLI output (Task 34) ---------
# apply_run already persists per-message failures (get_meta / batchModify
# errors) into RunJournal.failures() and returns EXIT_PARTIAL. Task 34 surfaces
# those ALREADY-STORED records read-only: the apply command prints a Failures
# section (count + each `message_id: reason` line) when the result is
# EXIT_PARTIAL, and summary always prints the section (a graceful `none` when
# there are no failures). Output may contain ONLY already-stored message ids
# and error strings — never sender/subject/body/size/content.


def _save_run_multi(tmp_path, mids) -> str:
    """Save a run with one candidate per message id (all under rule r1)."""
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    j.save_candidates(run_id, [
        Candidate(message_id=mid, thread_id=f"t-{mid}", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True),
                  before_labels={"INBOX"}, in_inbox=True)
        for mid in mids
    ])
    return run_id


def _inject_get_nth(api, fail_at_call: int, exc: Exception) -> None:
    """Make the n-th get() call raise a generic (non-auth) per-message error."""
    calls = {"n": 0}
    orig = api._handlers["get"]

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == fail_at_call:
            raise exc
        return orig(**kw)

    api._handlers["get"] = flaky


def test_apply_partial_shows_failures_section(tmp_path, monkeypatch):
    """apply with a per-message failure: exit 6 and a Failures section printing
    the count plus the stored message_id: reason line."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    api.add_message("m2", subject="newsletter", labels={"INBOX"})
    _mock_net(monkeypatch, api)
    run_id = _save_run_multi(tmp_path, ["m1", "m2"])
    # apply re-verifies each candidate via get_meta: m1 (call 1) applies,
    # m2 (call 2) raises -> recorded as 'message gone or unreadable'.
    _inject_get_nth(api, fail_at_call=2, exc=RuntimeError("gone"))
    result = runner.invoke(app, ["apply", "--run", run_id, "--yes"])
    assert result.exit_code == 6  # EXIT_PARTIAL preserved
    assert "Failures:" in result.output
    assert "1 failed message" in result.output  # the count
    assert "m2: message gone or unreadable" in result.output  # id: reason
    # m1 was still applied; the failure was recorded in the journal
    assert "Cleanup/N" in api.label_names_of("m1")


def test_apply_partial_failures_privacy(tmp_path, monkeypatch):
    """apply's failure output shows only stored message ids + reason strings —
    never sender, subject, body, size, or raw content."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    api.add_message("m2", subject="SECRET-SUBJECT", from_hdr="attacker@example.com",
                    size_kb=123.0, labels={"INBOX"})
    _mock_net(monkeypatch, api)
    run_id = _save_run_multi(tmp_path, ["m1", "m2"])
    _inject_get_nth(api, fail_at_call=2, exc=RuntimeError("boom"))
    result = runner.invoke(app, ["apply", "--run", run_id, "--yes"])
    assert result.exit_code == 6
    out = result.output
    assert "m2: message gone or unreadable" in out  # stored error string only
    assert "SECRET-SUBJECT" not in out
    assert "attacker@example.com" not in out
    assert "123.0" not in out
    assert "size" not in out.lower()


def test_summary_with_failures_shows_section(tmp_path, monkeypatch):
    """summary prints the journal's stored failures: count plus message_id:
    reason lines for every record, read-only, exit 0."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    j.save_candidates(run_id, [
        Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                  actions=Actions(add_label=["Cleanup/N"], archive=True), in_inbox=True),
    ])
    j.save_stats(run_id, {"evaluated": 1, "excluded": 0, "noop": 0, "candidates": 1})
    j.record_failure(run_id, "m1", "message gone or unreadable")
    j.record_failure(run_id, "m9", "rate limited")
    result = runner.invoke(app, ["summary", "--run", run_id])
    assert result.exit_code == 0
    assert "Failures:" in result.output
    assert "2 failed message" in result.output
    assert "m1: message gone or unreadable" in result.output
    assert "m9: rate limited" in result.output


def test_summary_no_failures_graceful(tmp_path, monkeypatch):
    """A run with no recorded failures: the section prints `none` and exits 0 —
    never a crash and never a spurious message-id leak from candidates."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    run_id = _save_run_with_stats(
        tmp_path,
        [Candidate(message_id="SECRET-MSG-ID-1", thread_id="SECRET-THREAD-ID",
                   rule_id="r1", actions=Actions(add_label=["Cleanup"], archive=True),
                   in_inbox=True)],
        stats={"evaluated": 1, "excluded": 0, "noop": 0, "candidates": 1},
    )
    result = runner.invoke(app, ["summary", "--run", run_id])
    assert result.exit_code == 0
    assert "Failures:" in result.output
    assert "none" in result.output.lower()
    # candidate message/thread ids stay private in summary (only stored FAILURE
    # records are ever surfaced, and this run has none)
    assert "SECRET-MSG-ID" not in result.output
    assert "SECRET-THREAD-ID" not in result.output
