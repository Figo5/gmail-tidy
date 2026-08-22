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
    """--all --rules r1 exhausts only r1; r2's checkpoint entry stays absent."""
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
    assert set(data["rules"].keys()) == {"r1"}  # only r1 scanned/exhausted
    assert data["rules"]["r1"]["exhausted"] is True


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
