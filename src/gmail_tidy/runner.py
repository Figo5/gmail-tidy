"""Headless single-shot runner for the `gmail-tidy run` command.

``run`` is the Task Scheduler-friendly entry point: it scans and applies in a
single invocation, reusing the exact scan/apply/audit/checkpoint engine of the
interactive commands, but it is built to never do anything that needs an
interactive console:

- it **never launches an OAuth browser** and never refreshes a token. It only
  verifies the cached ``token.json`` metadata already carries the write scopes
  (``require_write_scope_headless``) and fails with ``AuthError`` (exit 4)
  before any service is constructed if it does not;
- it **never prompts on stdin**: candidates are applied through
  ``actions.apply_run`` with a non-interactive confirm (always True). A
  scheduled task has no console to answer a prompt;
- it **never self-heals**: no automatic scope escalation or credential refresh.
  If the cached token is missing, read-only, or expired, the run fails cleanly
  and the operator re-runs ``gmail-tidy auth refresh`` once interactively.

Output hygiene is the same as the rest of the tool: callers of ``run_cycle``
print only aggregate counts, random run ids, and fixed status words — never
message ids, thread ids, or any message content.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from gmail_tidy import audit as audit_mod
from gmail_tidy import auth as auth_mod
from gmail_tidy.actions import apply_run, scan as build_scan
from gmail_tidy.checkpoint import checkpoint_path, load_checkpoint, save_checkpoint
from gmail_tidy.config import Config
from gmail_tidy.errors import EXIT_OK, EXIT_PARTIAL, AuthError
from gmail_tidy.gmail_client import GmailClient


@dataclass(frozen=True)
class RunOutcome:
    """Aggregate-only result of one scan+apply cycle.

    Never carries message or thread ids — only the exit code, the (random)
    run id, the candidate count, and a fixed status word.
    """

    exit_code: int
    run_id: str
    candidates: int
    status: str  # one of: "noop", "dry-run", "applied", "partial"


def require_write_scope_headless(cfg_dir: Path) -> None:
    """Headless write-scope gate for scheduled runs.

    Checks only the cached token file's persisted ``scopes`` metadata. It never
    launches a browser, never refreshes, never calls get_credentials or
    build_service. Raises ``AuthError`` (mapped to exit 4 by the caller) when
    the token is missing or does not carry the write scopes
    (``gmail.modify`` + ``gmail.labels``).

    The interactive command that *grants* write scope is
    ``gmail-tidy auth refresh``; scheduled ``run`` invocations only verify it.
    """
    token = auth_mod.token_path(cfg_dir)
    if not token.exists():
        raise AuthError(
            f"no cached token in {cfg_dir} — run `gmail-tidy auth refresh` once "
            "interactively (the `run` command never opens a browser)"
        )
    granted = auth_mod.scope_state(token)
    missing = sorted(set(auth_mod.SCOPE_WRITE) - granted)
    if missing:
        raise AuthError(
            f"cached token in {cfg_dir} lacks write scopes "
            f"({', '.join(missing)}) — run `gmail-tidy auth refresh` once "
            "interactively to grant write access before scheduling `run`"
        )


def headless_credentials(cfg_dir: Path):
    """Load the cached token as credentials with no browser and no refresh path.

    Callers pass the result to ``build_service`` (a pure construction). A
    token that was persisted with write scopes but is unreadable on disk fails
    with ``AuthError`` (exit 4) rather than triggering any OAuth flow.
    """
    from google.oauth2.credentials import Credentials

    token = auth_mod.token_path(cfg_dir)
    try:
        return Credentials.from_authorized_user_file(str(token))
    except Exception as exc:  # noqa: BLE001 — surface any corrupt-token parse as auth
        raise AuthError(
            f"cached token in {cfg_dir} is unreadable — run `gmail-tidy auth "
            f"refresh` once interactively: {exc}"
        ) from exc


def run_cycle(client: GmailClient, config: Config, cfg_dir: Path,
              limit: int | None = None,
              rules: list[str] | None = None,
              dry_run: bool = False) -> RunOutcome:
    """One scan -> (dry-run report | apply) cycle. Returns ``RunOutcome``.

    Reuses the existing engine unchanged: ``actions.scan`` for planning,
    ``checkpoint`` for resumable progress, ``audit.RunJournal`` for the run
    file, and ``actions.apply_run`` for the write. Persists exactly what the
    existing scan + apply commands already persist (checkpoint, run journal,
    run stats, audit log) — no new persisted state, no new Gmail surface.

    Behavior contract:
    - no candidates: applies nothing, returns exit 0 (status "noop").
    - dry_run: scans, persists the run journal like scan does, but never calls
      apply_run — returns 0 (status "dry-run") and writes nothing to Gmail.
    - candidates + not dry_run: calls apply_run with a non-interactive confirm
      (always True), so it can never block on stdin; returns the existing
      success / partial codes (0 / 6), and an AuthError from a mid-run 403
      propagates to the caller's auth exit path.
    """
    if rules:
        # Same semantics as the `scan` command's --rules filter: keep only the
        # named rules; unknown ids are ignored.
        config.rules = [r for r in config.rules if r.id in rules]
    journal = audit_mod.RunJournal(cfg_dir / "runs")
    cp_path = checkpoint_path(cfg_dir)
    cp = load_checkpoint(cp_path, config)
    candidates, new_cp, stats = build_scan(client, config, limit=limit, checkpoint=cp)
    save_checkpoint(cp_path, new_cp)
    run_id = journal.init_run()
    journal.save_candidates(run_id, candidates)
    journal.save_stats(run_id, asdict(stats))
    if not candidates:
        return RunOutcome(exit_code=EXIT_OK, run_id=run_id, candidates=0, status="noop")
    if dry_run:
        return RunOutcome(exit_code=EXIT_OK, run_id=run_id,
                          candidates=len(candidates), status="dry-run")
    audit = audit_mod.AuditLog(cfg_dir / "audit.jsonl")
    exit_code = apply_run(client, config, candidates, journal, audit, run_id,
                          confirm=lambda: True)  # noninteractive: never reads stdin
    status = "partial" if exit_code == EXIT_PARTIAL else "applied"
    return RunOutcome(exit_code=exit_code, run_id=run_id,
                      candidates=len(candidates), status=status)
