# tests/test_runner.py
"""Offline tests for the headless `gmail-tidy run` command and runner module.

Every test runs through the Typer CliRunner with the CLI's two module-level
hooks (build_service / headless credentials) monkeypatched, so nothing touches
a network, an OAuth browser flow, or real credentials. The `run` command must
never call cli.get_credentials / auth.get_credentials (which can launch a
browser) — the test doubles prove exactly that.
"""

import json

from typer.testing import CliRunner

from gmail_tidy import cli
from gmail_tidy import config as config_mod
from gmail_tidy.audit import AuditLog, RunJournal
from gmail_tidy.checkpoint import config_fingerprint
from gmail_tidy.cli import app
from gmail_tidy.errors import EXIT_PARTIAL
from gmail_tidy import runner as runner_mod
from tests.mock_gmail import MockGmailApi

runner = CliRunner()

CONFIG = (
    "rules:\n"
    "  - id: r1\n"
    "    match: {subject_contains: [newsletter]}\n"
    "    actions:\n"
    "      add_label: [Cleanup/N]\n"
    "      archive: true\n"
)

WRITE_TOKEN = json.dumps({
    "token": "fake-token",
    "refresh_token": "fake-refresh",
    "client_id": "x",
    "client_secret": "x",
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.labels",
    ],
})

READ_TOKEN = json.dumps(
    {
        "token": "fake-token",
        "refresh_token": "fake-refresh",
        "client_id": "x",
        "client_secret": "x",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    }
)


def _write_token(tmp_path, text: str = WRITE_TOKEN) -> None:
    (tmp_path / "token.json").write_text(text, encoding="utf-8")


def _setup(tmp_path, monkeypatch, api=None, token=WRITE_TOKEN):
    """Standard offline run harness: config + write-scope token + mock net."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    _write_token(tmp_path, token)
    api = api or MockGmailApi()
    # The run command builds the service from headless credentials it loads
    # itself, then passes the service to cli.build_service. Patch the builder
    # to return the in-memory mock.
    monkeypatch.setattr(cli, "build_service", lambda creds: api)
    return api


# --- headless scope gate ---------------------------------------------------


def test_gate_missing_token_raises_before_any_service(tmp_path):
    import pytest
    from gmail_tidy.errors import AuthError

    with pytest.raises(AuthError):
        runner_mod.require_write_scope_headless(tmp_path)


def test_gate_readonly_token_raises(tmp_path):
    import pytest
    from gmail_tidy.errors import AuthError

    _write_token(tmp_path, READ_TOKEN)
    with pytest.raises(AuthError):
        runner_mod.require_write_scope_headless(tmp_path)


def test_gate_corrupt_token_raises(tmp_path):
    import pytest
    from gmail_tidy.errors import AuthError

    (tmp_path / "token.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(AuthError):
        runner_mod.require_write_scope_headless(tmp_path)


def test_gate_write_token_passes(tmp_path):
    _write_token(tmp_path)
    runner_mod.require_write_scope_headless(tmp_path)  # no exception


# --- command-level: auth gating ------------------------------------------------


def test_run_missing_config_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2
    assert "no config" in result.output.lower()


def test_run_missing_token_exits_4_no_credentials(tmp_path, monkeypatch):
    """Missing cached token -> exit 4, and get_credentials (the browser path)
    must never be called."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")

    def _boom(*a, **k):
        raise AssertionError("run must not call get_credentials (browser OAuth)")

    monkeypatch.setattr(cli, "get_credentials", _boom)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 4
    assert "auth refresh" in result.output


def test_run_readonly_token_exits_4(tmp_path, monkeypatch):
    """A cached read-only token must not be reused for a write run: exit 4,
    never building a service."""
    _setup(tmp_path, monkeypatch, token=READ_TOKEN)

    def _boom(*a, **k):
        raise AssertionError("run must not build a service on an insufficient token")

    monkeypatch.setattr(cli, "build_service", _boom)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 4
    assert "write scopes" in result.output.lower()


def test_run_missing_config_takes_precedence_over_token(tmp_path, monkeypatch):
    """Even with a valid write token, a missing config is a config error (exit 2)
    — matching the scan command's precedence."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    _write_token(tmp_path)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2


# --- CLI: apply path ------------------------------------------------------------


def test_run_applies_and_exits_zero(tmp_path, monkeypatch):
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    _setup(tmp_path, monkeypatch, api)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert "applied" in result.output
    assert "1 candidate" in result.output
    assert "Cleanup/N" in api.label_names_of("m1")
    assert "INBOX" not in api.label_names_of("m1")


def test_run_no_candidates_exits_zero(tmp_path, monkeypatch):
    api = MockGmailApi()
    api.add_message("m1", subject="receipt", labels={"INBOX"})  # no rule matches
    _setup(tmp_path, monkeypatch, api)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert "0 candidate" in result.output
    assert "Cleanup/N" not in api.label_names_of("m1")


def test_run_no_candidates_still_persists_run_and_checkpoint(tmp_path, monkeypatch):
    """A 0-candidate run must still write a run journal + checkpoint (same as
    scan) so the next run advances past the empty page."""
    api = MockGmailApi()
    api.add_message("m1", subject="receipt", labels={"INBOX"})
    _setup(tmp_path, monkeypatch, api)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert len(RunJournal(tmp_path / "runs").list_runs()) == 1
    assert (tmp_path / "checkpoint.json").exists()


def test_run_dry_run_never_writes(tmp_path, monkeypatch):
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    _setup(tmp_path, monkeypatch, api)
    result = runner.invoke(app, ["run", "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output
    assert "Cleanup/N" not in api.label_names_of("m1")
    assert "INBOX" in api.label_names_of("m1")
    # dry-run must still journal the plan (like scan does)
    assert len(RunJournal(tmp_path / "runs").list_runs()) == 1


def test_run_never_prompts(tmp_path, monkeypatch):
    """A scheduled run has no stdin; any prompt read would hang. Confirm that
    apply is noninteractive by forbidding typer.confirm entirely."""
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    _setup(tmp_path, monkeypatch, api)

    def _fail_if_confirm(prompt):
        raise AssertionError("run must never prompt on stdin")

    monkeypatch.setattr(cli.typer, "confirm", _fail_if_confirm)
    # No input= provided: a real prompt would hang, and confirm() would raise.
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert "Cleanup/N" in api.label_names_of("m1")


def test_run_partial_failure_exits_six(tmp_path, monkeypatch):
    """Some batches failed -> exit 6 with a 'partial' status word."""
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    api.add_message("m2", subject="newsletter", labels={"INBOX"})
    _setup(tmp_path, monkeypatch, api)

    # Fail the 4th get() call: scan re-verifies m1,m2 (calls 1-2), then apply
    # re-verifies m1 (call 3, succeeds) and m2 (call 4, fails) — a partial.
    calls = {"n": 0}
    orig_get = api._get

    def flaky_get(**params):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("boom")
        return orig_get(**params)

    api._handlers["get"] = flaky_get
    result = runner.invoke(app, ["run"])
    assert result.exit_code == EXIT_PARTIAL
    assert "partial" in result.output
    # m1 was applied; m2 was recorded as a failure in the run journal
    assert "Cleanup/N" in api.label_names_of("m1")
    run_id = RunJournal(tmp_path / "runs").list_runs()[0]
    assert len(RunJournal(tmp_path / "runs").failures(run_id)) == 1


def test_run_audits_its_actions(tmp_path, monkeypatch):
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    _setup(tmp_path, monkeypatch, api)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    entries = AuditLog(tmp_path / "audit.jsonl").entries()
    assert len(entries) == 2  # add_label + archive
    assert entries[0].run_id == RunJournal(tmp_path / "runs").list_runs()[0]


# --- privacy of run output -------------------------------------------------------


def test_run_output_has_no_message_or_thread_ids(tmp_path, monkeypatch):
    api = MockGmailApi()
    api.add_message("SECRET-MSG-ID-1", subject="newsletter", labels={"INBOX"})
    api.add_message("SECRET-MSG-ID-2", subject="newsletter", labels={"INBOX"})
    _setup(tmp_path, monkeypatch, api)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert "SECRET-MSG-ID" not in result.output
    assert "SECRET-THREAD-ID" not in result.output
    # aggregate counts + rule id + status word still visible
    assert "2 candidate" in result.output
    assert "applied" in result.output


def test_run_dry_run_output_no_ids(tmp_path, monkeypatch):
    api = MockGmailApi()
    api.add_message("SECRET-MSG-ID-1", subject="newsletter", labels={"INBOX"})
    _setup(tmp_path, monkeypatch, api)
    result = runner.invoke(app, ["run", "--dry-run"])
    assert result.exit_code == 0
    assert "SECRET-MSG-ID" not in result.output


def test_run_help_exits_zero_offline(tmp_path, monkeypatch):
    """--help must render without touching config, tokens, or Gmail."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0


# --- direct run_cycle unit tests -----------------------------------------------


def test_run_cycle_respects_rules_filter(tmp_path, monkeypatch):
    """--rules filters the config's rules before scanning (same as scan)."""
    cfg = (
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
    api = MockGmailApi()
    api.add_message("m1", subject="alpha", labels={"INBOX"})
    api.add_message("m2", subject="beta", labels={"INBOX"})
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(cfg, encoding="utf-8")
    _write_token(tmp_path)
    monkeypatch.setattr(cli, "build_service", lambda creds: api)
    result = runner.invoke(app, ["run", "--rules", "r1"])
    assert result.exit_code == 0
    assert "1 candidate" in result.output
    assert "Cleanup/A" in api.label_names_of("m1")
    assert "Cleanup/B" not in api.label_names_of("m2")


def test_run_cycle_rules_subset_preserves_unselected_rules_checkpoint(tmp_path, monkeypatch):
    """Regression: run_cycle with --rules scoping must preserve the unselected
    rule's checkpoint entry and keep the full-config fingerprint — a scoped run
    must never silently drop another rule's resume state. --dry-run keeps the
    test fully read-only w.r.t. the mock mailbox (no apply)."""
    cfg_text = (
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
    api = MockGmailApi()
    api.add_message("a1", subject="alpha", labels={"INBOX"})
    api.add_message("a2", subject="alpha", labels={"INBOX"})
    api.add_message("b1", subject="beta", labels={"INBOX"})
    api.add_message("b2", subject="beta", labels={"INBOX"})
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(cfg_text, encoding="utf-8")
    _write_token(tmp_path)
    monkeypatch.setattr(cli, "build_service", lambda creds: api)

    # unscoped dry-run run: both rules scanned, checkpoint holds r1+r2
    result = runner.invoke(app, ["run", "--dry-run"])
    assert result.exit_code == 0
    assert "4 candidate" in result.output
    data1 = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert set(data1["rules"].keys()) == {"r1", "r2"}
    fp = config_fingerprint(config_mod.load_config(tmp_path / "config.yaml"))

    # scoped dry-run run: r1 re-scanned, r2's prior entry preserved unchanged
    result2 = runner.invoke(app, ["run", "--rules", "r1", "--dry-run"])
    assert result2.exit_code == 0
    data2 = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert set(data2["rules"].keys()) == {"r1", "r2"}   # r2 NOT dropped
    assert data2["rules"]["r2"] == data1["rules"]["r2"]  # r2 unchanged
    assert data2["config_fingerprint"] == fp             # full-config hash kept


def test_run_cycle_limit_stops_at_limit(tmp_path, monkeypatch):
    api = MockGmailApi()
    for i in range(4):
        api.add_message(f"m{i}", subject="newsletter", labels={"INBOX"})
    _setup(tmp_path, monkeypatch, api)
    result = runner.invoke(app, ["run", "--limit", "2"])
    assert result.exit_code == 0
    assert "2 candidate" in result.output
    assert sum("Cleanup/N" in api.label_names_of(f"m{i}") for i in range(4)) == 2


def test_run_cycle_dry_run_never_reaches_apply_run(tmp_path, monkeypatch):
    """--dry-run must short-circuit before apply_run: prove it by making
    apply_run explode — a dry run never calls it."""
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    _setup(tmp_path, monkeypatch, api)

    def _boom_apply(*a, **k):
        raise AssertionError("dry-run must never call apply_run")

    monkeypatch.setattr(runner_mod, "apply_run", _boom_apply)
    result = runner.invoke(app, ["run", "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output
    # the mock mailbox is untouched
    assert "Cleanup/N" not in api.label_names_of("m1")


def test_run_cycle_uses_headless_credentials_not_get_credentials(tmp_path, monkeypatch):
    """The service is built from runner.headless_credentials, never from
    cli.get_credentials (which can open a browser)."""
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    _setup(tmp_path, monkeypatch, api)

    def _boom(*a, **k):
        raise AssertionError("run must use headless_credentials, not get_credentials")

    monkeypatch.setattr(cli, "get_credentials", _boom)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0

