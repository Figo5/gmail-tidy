"""Typer command surface.

All commands talk to Gmail by design; write commands require confirmation
(--yes bypasses); preview/undo default to dry-run (no writes).
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from gmail_tidy import audit as audit_mod
from gmail_tidy import auth as auth_mod
from gmail_tidy import config as config_mod
from gmail_tidy.actions import apply_run, scan as build_scan  # alias: command named scan below
from gmail_tidy.checkpoint import checkpoint_path, load_checkpoint, save_checkpoint
from gmail_tidy.errors import (
    AuthError, ConfigError, NoWorkError,
    EXIT_OK, EXIT_RUNTIME, EXIT_CONFIG, EXIT_NOOP, EXIT_AUTH, EXIT_CANCELLED,
)
from gmail_tidy.gmail_client import GmailClient
from gmail_tidy.undo import build_undo_plan, execute_undo

app = typer.Typer(add_completion=False)
console = Console()


def build_service(creds):
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=creds)


def get_credentials(cfg_dir: Path, require_write: bool):
    return auth_mod.get_credentials(cfg_dir, cfg_dir / "client_secret.json", require_write=require_write)


def _load_config() -> tuple[Path, config_mod.Config]:
    cfg_dir = config_mod.config_dir()
    path = cfg_dir / "config.yaml"
    if not path.exists():
        raise ConfigError(f"no config at {path} — run `gmail-tidy init`")
    return cfg_dir, config_mod.load_config(path)


def _client(cfg_dir: Path, require_write: bool) -> GmailClient:
    return GmailClient(build_service(get_credentials(cfg_dir, require_write)))


def _latest_run(journal: audit_mod.RunJournal) -> str | None:
    runs = journal.list_runs()
    return runs[-1] if runs else None


def _preview_undo(run_id: str, plan) -> None:
    """Print the inverse plan (dry-run / preview path). Always exits EXIT_OK."""
    console.print(f"inverse plan for run {run_id} (dry-run):")
    for inv in plan:
        console.print(f"  {inv.message_id}: +{inv.add_label} -{inv.remove_label} "
                      f"inbox={inv.re_inbox}")
    raise typer.Exit(EXIT_OK)


def _exit_for(err: Exception) -> int:
    if isinstance(err, ConfigError):
        return EXIT_CONFIG
    if isinstance(err, AuthError):
        return EXIT_AUTH
    if isinstance(err, NoWorkError):
        return EXIT_NOOP
    return EXIT_RUNTIME


@app.command()
def init():
    """Create the config dir + template and start read-only OAuth."""
    try:
        cfg_dir = config_mod.ensure_config_dir()
        conf = cfg_dir / "config.yaml"
        if not conf.exists():
            conf.write_text(config_mod.default_template(), encoding="utf-8")
            console.print(f"[green]wrote[/green] {conf}")
        get_credentials(cfg_dir, require_write=False)
        console.print("[green]authenticated with read-only scope.[/green]")
        raise typer.Exit(EXIT_OK)
    except AuthError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_AUTH)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_CONFIG)


@app.command()
def scan(limit: int | None = typer.Option(None, "--limit"),
         all_: bool = typer.Option(False, "--all",
                                   help="Scan the entire mailbox to exhaustion (mutually exclusive with --limit)"),
         rules: list[str] = typer.Option(None, "--rules")):
    """Build a candidate plan (read-only) and write it to the run journal."""
    try:
        cfg_dir, cfg = _load_config()
        if all_ and limit is not None:
            raise ConfigError("--all cannot be combined with --limit")
        if rules:
            cfg.rules = [r for r in cfg.rules if r.id in rules]
        client = _client(cfg_dir, require_write=False)
        # Load the persisted pagination checkpoint so this scan resumes where
        # the last one left off instead of restarting at page 1.
        cp_path = checkpoint_path(cfg_dir)
        cp = load_checkpoint(cp_path, cfg)

        def _on_progress(cp, rule_id, n_candidates):
            # Incremental checkpoint save + progress line during a long --all
            # run. The progress line is deliberately free of any message id,
            # thread id, subject, sender, or body — only the rule id (a
            # user-configured, non-sensitive string) and an integer count.
            save_checkpoint(cp_path, cp)
            console.print(f"[dim]rule {rule_id}: done ({n_candidates} candidates so far)[/dim]")

        candidates, new_cp, _stats = build_scan(
            client, cfg, limit=limit, checkpoint=cp, full=all_,
            on_progress=_on_progress if all_ else None,
        )
        # Always persist the new checkpoint — even with 0 candidates — so the
        # NEXT invocation continues past an empty page instead of re-fetching it.
        save_checkpoint(cp_path, new_cp)
        # Always create a run file, even for 0 candidates, so preview's
        # _latest_run() never falls back to a stale already-applied run.
        journal = audit_mod.RunJournal(cfg_dir / "runs")
        run_id = journal.init_run()
        journal.save_candidates(run_id, candidates)
        if not candidates:
            console.print("nothing matched the configured rules.")
            raise typer.Exit(EXIT_NOOP)
        console.print(f"[green]scan complete[/green]: {len(candidates)} candidate(s) — run {run_id}")
        raise typer.Exit(EXIT_OK)
    except (ConfigError, AuthError, NoWorkError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(_exit_for(e))


@app.command()
def preview(run: str | None = typer.Option(None, "--run")):
    """Render a run's proposed actions (dry-run, no writes)."""
    try:
        cfg_dir, _cfg = _load_config()
        journal = audit_mod.RunJournal(cfg_dir / "runs")
        run_id = run or _latest_run(journal)
        if not run_id:
            console.print("no run found — run `gmail-tidy scan` first.")
            raise typer.Exit(EXIT_NOOP)
        candidates = journal.load_candidates(run_id)
        table = Table(title=f"Run {run_id} — proposed actions (dry-run)")
        table.add_column("id")
        table.add_column("rule")
        table.add_column("actions")
        for c in candidates:
            acts: list[str] = []
            if c.actions.add_label:
                acts.append("+".join(c.actions.add_label))
            if c.actions.remove_label:
                acts.append("-".join(c.actions.remove_label))
            if c.actions.archive:
                acts.append("archive")
            table.add_row(c.message_id, c.rule_id, ", ".join(acts))
        console.print(table)
        console.print(f"[dim]{len(candidates)} message(s). Apply with `gmail-tidy apply --yes`.[/dim]")
        raise typer.Exit(EXIT_OK)
    except (ConfigError, AuthError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(_exit_for(e))


@app.command()
def apply(run_id: str | None = typer.Option(None, "--run"),
          yes: bool = typer.Option(False, "--yes")):
    """Re-verify and execute a run's actions (the only write command)."""
    try:
        cfg_dir, cfg = _load_config()
        journal = audit_mod.RunJournal(cfg_dir / "runs")
        run_id = run_id or _latest_run(journal)
        if not run_id:
            console.print("no run found — run `gmail-tidy scan` first.")
            raise typer.Exit(EXIT_NOOP)
        candidates = journal.load_candidates(run_id)
        if not candidates:
            console.print("run has no candidates.")
            raise typer.Exit(EXIT_NOOP)
        client = _client(cfg_dir, require_write=True)  # escalate scope before any write
        audit = audit_mod.AuditLog(cfg_dir / "audit.jsonl")
        console.print(f"[yellow]{len(candidates)} message(s) will be modified.[/yellow]")
        confirm = (lambda: True) if yes else (lambda: typer.confirm("Proceed with apply?"))
        result = apply_run(client, cfg, candidates, journal, audit, run_id, confirm)
        if result == EXIT_CANCELLED:
            console.print("cancelled.")
        raise typer.Exit(result)
    except (ConfigError, AuthError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(_exit_for(e))


@app.command()
def undo(run_id: str,
         apply_: bool = typer.Option(False, "--apply",
                                     help="Signal real write-intent; see the gate below."),
         yes: bool = typer.Option(False, "--yes"),
         dry_run: bool = typer.Option(False, "--dry-run")):
    """Reverse a run's actions; dry-run by default, idempotent.

    Behavior (precedence: dry_run > yes-without-apply usage error > default
    preview > --apply prompt > --apply --yes write):

    - no flags or --dry-run: print the inverse plan and exit 0 (never writes).
    - --apply: print the plan, then prompt "Proceed with undo?"; on decline
      print "cancelled." and exit 5, on accept write.
    - --apply --yes: write immediately with no prompt (for automation); never
      reads stdin.
    - --yes WITHOUT --apply: usage error (exit 2) — a nonsensical combination,
      rejected rather than silently ignored.
    """
    try:
        cfg_dir = config_mod.config_dir()
        journal = audit_mod.RunJournal(cfg_dir / "runs")
        candidates = journal.load_candidates(run_id)
        plan = [inv for c in candidates for inv in build_undo_plan(c)]
        # Precedence per the gate contract:
        #   (1) dry_run wins over everything (--dry-run --yes previews, never errors).
        #   (2) --yes without --apply is a usage error, not a silent no-op.
        #   (3) not apply_ (and not yes, handled by 2) → preview.
        #   (4) apply_ and not yes → confirm-then-write.
        #   (5) apply_ and yes → write immediately, never reading stdin.
        if dry_run:
            _preview_undo(run_id, plan)
        elif not apply_ and yes:
            raise ConfigError("--yes requires --apply")
        elif not apply_:
            _preview_undo(run_id, plan)
        else:
            client = _client(cfg_dir, require_write=True)
            audit = audit_mod.AuditLog(cfg_dir / "audit.jsonl")
            confirm = (lambda: True) if yes else (lambda: typer.confirm("Proceed with undo?"))
            result = execute_undo(client, plan, audit, run_id, confirm)
            if result == EXIT_CANCELLED:
                console.print("cancelled.")
            raise typer.Exit(result)
    except (ConfigError, AuthError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(_exit_for(e))
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_CONFIG)


@app.command()
def status():
    """Account, scopes, run history, audit-log path."""
    try:
        cfg_dir = config_mod.config_dir()
        conf = cfg_dir / "config.yaml"
        token = cfg_dir / "token.json"
        scopes = auth_mod.scope_state(token)
        runs = audit_mod.RunJournal(cfg_dir / "runs").list_runs()
        console.print(f"config dir : {cfg_dir}")
        console.print(f"config     : {'present' if conf.exists() else 'MISSING'}")
        console.print(f"token      : {'present' if token.exists() else 'absent'}")
        console.print(f"scopes     : {sorted(scopes) or '(none)'}")
        console.print(f"last run   : {runs[-1] if runs else '(none)'}")
        console.print(f"run count  : {len(runs)}")
        console.print(f"audit log  : {cfg_dir / 'audit.jsonl'}")
        raise typer.Exit(EXIT_OK)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_CONFIG)


auth_app = typer.Typer(help="Manage OAuth tokens and scopes.")
app.add_typer(auth_app, name="auth")


@auth_app.command("status")
def auth_status():
    try:
        cfg_dir = config_mod.config_dir()
        scopes = auth_mod.scope_state(cfg_dir / "token.json")
        console.print(f"token  : {cfg_dir / 'token.json'}")
        console.print(f"scopes : {sorted(scopes) or '(none)'}")
        raise typer.Exit(EXIT_OK)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_CONFIG)


@auth_app.command("refresh")
def auth_refresh():
    try:
        cfg_dir = config_mod.config_dir()
        auth_mod.upgrade_write(cfg_dir, cfg_dir / "client_secret.json")
        console.print("[green]token refreshed with write scopes.[/green]")
        raise typer.Exit(EXIT_OK)
    except AuthError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_AUTH)


@auth_app.command("revoke")
def auth_revoke():
    try:
        cfg_dir = config_mod.config_dir()
        auth_mod.revoke(cfg_dir)
        console.print("[green]local token removed. Server-side token remains until it expires.[/green]")
        raise typer.Exit(EXIT_OK)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_CONFIG)
