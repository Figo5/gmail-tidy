# gmail-tidy

A privacy-conscious Python CLI that applies **declarative cleanup rules** to your
**existing** Gmail messages. Rules match metadata only (sender, subject, labels, size,
age, read state) and the only actions are **add/remove labels** and **archive**
(remove from INBOX).

gmail-tidy **never deletes, trashes, marks as spam, sends, or imports mail** — that
surface is blocked by a precise AST-level test, not just convention.

[![CI](https://github.com/Figo5/gmail-tidy/actions/workflows/ci.yml/badge.svg)](https://github.com/Figo5/gmail-tidy/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://github.com/Figo5/gmail-tidy)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/Figo5/gmail-tidy/blob/main/LICENSE)

> **Install from GitHub source only.** gmail-tidy is **not yet published to PyPI** —
> `pip install gmail-tidy` will **not** work (it fails with "No matching distribution
> found"). Install from a local clone of this repository as described in
> [Install](#install).

## Why

Most mail-cleanup tools either only apply filters to *future* mail, or are
delete-first utilities. gmail-tidy combines three things no v1 competitor does:

1. **Declarative rules over existing mail** — `scan` builds a local plan, `apply` is
   the only command that writes.
2. **Zero destructive actions** — the only write is `users.messages.batchModify`
   (label add/remove, ≤ 1000 ids/batch).
3. **Global include/exclude guardrails** — protected labels and excluded senders are
   never touched, and this is re-checked immediately before every write.

## Install

gmail-tidy is installed **from source** (a local clone of this repository) — it is
not on PyPI. Requires Python **3.11+**. The package uses `google-auth-oauthlib` +
`google-api-python-client` for OAuth2, `typer`/`rich` for the CLI, and
`pydantic`/`PyYAML` for config validation.

### Windows (PowerShell)

These steps assume you already have a clone of this repository on disk — just `cd`
into it. (If you don't have one yet, clone it first, then run the rest.)

```powershell
# 1. cd into your existing clone
cd C:\path\to\gmail-tidy

# 2. Create a virtual environment with the Python launcher.
#    `py -0p` lists the Python versions you have installed; any 3.11+ works.
py -3.11 -m venv .venv

# 3. Activate the venv (PowerShell).
.\.venv\Scripts\Activate.ps1
#    If activation is blocked by the execution policy, run this once for the
#    current shell, then re-run the Activate.ps1 line above:
#    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 4. Install. Use the editable + dev extras form for development:
pip install -e ".[dev]"
#    ...or the plain editable install if you only want to run it:
#    pip install -e .

# 5. Verify.
gmail-tidy --help
#    (or, without installing: python -m gmail_tidy --help)
```

> **POSIX (macOS/Linux):** the same flow works with `python3 -m venv .venv`,
> `source .venv/bin/activate`, and `pip install -e ".[dev]"`.

## Quick start — first safe workflow

Work through these in order. The example below uses **label-only rules** (add a
label, archive) — no destructive actions — so it is safe to run against a real
mailbox.

1. **Cloud setup:** create your own Google Cloud OAuth client per
   [docs/google-cloud-setup.md](docs/google-cloud-setup.md) and download
   `client_secret.json` into the config dir. It is **never committed** and never
   hardcoded.
2. **Init:** `gmail-tidy init` — creates the config dir
   (`~/.config/gmail-tidy/`, or `$GMAIL_TIDY_CONFIG`), writes a commented config
   template, and authenticates with the **read-only** scope.
3. **Edit config:** enable rules in `config.yaml`. Presets ship **disabled by
   default** — a fresh install's `scan` changes nothing. See
   [docs/config-reference.md](docs/config-reference.md). All examples in this repo
   use only synthetic addresses (`example.com`):

   ```yaml
   rules:
     - id: old-unread-newsletters
       match:
         subject_contains: ["newsletter", "digest"]
         older_than_days: 30
       actions:
         add_label: ["Cleanup/Newsletters"]
         archive: true
   ```
4. **Scan:** `gmail-tidy scan` builds a candidate plan (read-only) and writes a local
   run file. If nothing matches, it prints `nothing matched the configured rules.`
   and exits `3`.
5. **Preview:** `gmail-tidy preview` renders the proposed actions (dry-run, no
   writes). It defaults to the latest run.
6. **Apply — without `--yes` first.** `gmail-tidy apply` re-verifies every message
   against current state, then shows you exactly what it will do and asks for
   confirmation:

   ```
   $ gmail-tidy apply
   12 message(s) will be modified.
   Proceed with apply? [y/N]:
   ```

   - Type `y` → it executes (escalating to `gmail.modify` + `gmail.labels` via a
     fresh consent prompt if needed).
   - Type anything else / press Enter (the default is **No**) → it prints
     `cancelled.` and exits `5`, writing nothing.
   - `gmail-tidy apply --yes` skips the prompt entirely and executes immediately.
7. **Check the result:** `gmail-tidy status` shows your config dir, token/scopes,
   last run, run count, and the audit-log path. The audit log
   (`audit.jsonl` in the config dir) records every action it took.
8. **Undo — dry-run by default, no flags needed.** `gmail-tidy undo <run_id>`
   **always** prints the inverse plan and exits `0` — that *is* the dry-run; you do
   **not** need `--dry-run` to preview it. A real write requires the **`--apply`**
   flag: `gmail-tidy undo <run_id> --apply` prints the plan and then prompts for
   confirmation (decline prints `cancelled.` and exits `5`), while
   `gmail-tidy undo <run_id> --apply --yes` writes immediately with no prompt (for
   automation; it never reads stdin). `--yes` **without** `--apply` is a usage
   error (exit `2`). Undo is idempotent and **skips messages you changed since**
   the run (see [docs/safety-and-privacy.md](docs/safety-and-privacy.md)).

## Scan semantics: `--limit`, pagination, and checkpoint progress

`gmail-tidy scan` is read-only and **resumes forward progress through your
mailbox** across invocations:

- **`--limit N` means "up to N *new eligible* candidates"**, not raw messages
  fetched. Already-labeled / no-op / excluded messages do **not** count toward
  `N`, so with several rules `scan --limit 500` returns at most 500 candidates
  in total (across all rules), regardless of how many messages Gmail lists.
- **Scan skips what's already done.** Messages that already carry the target
  `Cleanup/*` label, are already archived, or are excluded are silently passed
  over, and scanning continues deeper into the mailbox instead of stopping at
  page 1.
- **Progress is checkpointed.** Each scan records, per rule, the Gmail
  `pageToken` it reached in `checkpoint.json` inside the config directory
  (`~/.config/gmail-tidy/`, or `$GMAIL_TIDY_CONFIG`). Running `scan --limit N`
  repeatedly makes forward progress through the mailbox — each run continues
  past the already-processed pages instead of re-fetching them from the start —
  until the mailbox is exhausted (after which scans find only genuinely new
  mail).
- **Editing `config.yaml` resets progress — that's expected, not a bug.** The
  checkpoint is keyed to a hash of your rules and `protect.include`/`exclude`;
  any edit invalidates it and the next scan restarts from page 1. This is
  deliberate: a stale page token could otherwise silently skip messages under
  the new rules.
- **Safety is unchanged.** `scan` is fully read-only; only `apply`/`undo` write
  to Gmail. The checkpoint file only records opaque Gmail pagination tokens and
  rule ids — no message content or headers.

## Batch processing across large mailboxes

For a large mailbox, process it in chunks rather than one giant `scan`:

```bash
gmail-tidy scan --limit 200     # first 200 new eligible candidates
gmail-tidy preview              # review what it found
gmail-tidy apply --yes          # apply (or apply without --yes to confirm)
gmail-tidy scan --limit 200     # resumes where the last scan left off
gmail-tidy apply --yes
# ... repeat ...
```

Each `scan --limit 200` **automatically resumes** from the previous scan's
`checkpoint.json` (in the config dir) — it does **not** re-scan messages it already
processed. Keep repeating until `scan` reports **0 new candidates** (exit `3`,
`nothing matched the configured rules.`), which means the mailbox is exhausted for
your current rules.

- **`checkpoint.json` lives in the config dir** (`~/.config/gmail-tidy/`, or
  `$GMAIL_TIDY_CONFIG`) and records, per rule, the Gmail `pageToken` reached.
- **Any `config.yaml` edit resets progress automatically** — the checkpoint is keyed
  to a hash of your rules and `protect.include`/`exclude`, so editing the config
  invalidates it and the next scan restarts from page 1. This is deliberate and
  safe (a stale page token under new rules could silently skip messages).
- **To force a full re-scan without editing config:** delete `checkpoint.json` and
  the next `scan` restarts from page 1.

## OAuth scope escalation

`scan`, `preview`, and `status` use only the **read-only** scope
(`gmail.readonly`). `apply` and `undo` escalate to **write** scope
(`gmail.modify` + `gmail.labels`) automatically on first use, via a fresh consent
prompt. A cached read-only token is **never silently reused** for a write
operation — if the stored token lacks the required scopes, the CLI deletes it and
re-prompts for consent. After escalation, the single token covers both read and
write commands.

> **Testing-mode tokens expire after 7 days.** Because the OAuth consent screen is
> in **Testing** publishing status (required while the app is unverified), Google
> expires refresh tokens after 7 days of Testing-mode use. Expect to re-authenticate
> roughly weekly: `gmail-tidy auth refresh` (for write scope) or a fresh
> `gmail-tidy init` / `scan` (for read scope). This is the most common real-world
> "why did this suddenly need re-auth?" surprise. Full detail:
> [docs/google-cloud-setup.md](docs/google-cloud-setup.md).

## Safety model

- **Plan vs. play:** `scan`/`preview` never write. `apply` is the only write command
  and re-checks `exclude` against live state immediately before each batch.
- **No destructive surface:** delete / trash / spam-report / send / import are absent
  from the code and enforced by an AST test over `src/`, plus a mock that exposes only
  the allowed Gmail surface.
- **Protected labels are never touched:** `IMPORTANT`, `STARRED`, `SPAM`, `TRASH`,
  `DRAFT`, `SENT`, `CHAT`, and tool-created `Cleanup/*` labels can never be removed
  or modified; a rule that names one fails config validation (exit 2). Gmail
  **system labels** (`INBOX`, `UNREAD`, `STARRED`, ...) can also never be named in
  `add_label` — the tool only adds **user** labels. The one exception is
  `remove_label: [INBOX]`, which is the explicit form of archiving.
- **Minimal data:** the audit log and run files store only IDs and label operations —
  never senders, subjects, bodies, sizes, or content.

## What this tool will never do

- **Delete** or **trash** messages
- **Permanently delete**, **batch-delete**, or **import** mail
- Mark mail as **spam**
- **Send** or **draft** mail
- Watch/push/stop (no `users.watch`/`stop`, no `drafts.*`)
- Touch anything via `users.settings.*` or `threads.delete`

The complete, audited API surface is `users.messages.list/get/batchModify`,
`users.labels.list/get/create`, and `users.getProfile`.

## CLI

Every command talks to Gmail by design — "dry-run" means **no writes**, not no
network.

| Command | Behavior |
|---|---|
| `init` | Create config dir + commented template (presets disabled), start read-only OAuth |
| `scan [--limit N] [--rules ID...]` | Build candidate plan → local run file; prints counts only. `--limit N` caps the plan at **N new eligible candidates** (not raw messages fetched). Pagination resumes from a saved checkpoint each run |
| `preview [--run ID]` | Render a run's proposed actions (dry-run, no writes) |
| `apply [--run ID] [--yes]` | Re-verify → confirm → execute in batches → journal → audit log |
| `undo <run ID> [--apply] [--yes]` | Reverse a run's actions from its before-state snapshot. **Dry-run by default** — with no flags it prints the inverse plan and exits `0`. A write requires `--apply`: `--apply` alone prompts for confirmation (decline exits `5`), `--apply --yes` writes immediately with no prompt. `--yes` without `--apply` is a usage error (exit 2). Idempotent |
| `status` | Account, scopes held, last run, run history, audit-log path |
| `auth status` / `auth refresh` / `auth revoke` | Inspect / escalate (read-only → `modify` + `labels`) / revoke tokens |

**Exit codes:** `0` success · `1` runtime error · `2` config/usage · `3` nothing to
do · `4` auth error · `5` cancelled by user · `6` partial success (some batches
failed; resume with `apply`).

## Windows Task Scheduler

You can schedule gmail-tidy to run cleanup on a timer. Two hard constraints:

- **Only schedule label-only rules.** gmail-tidy's only actions are
  `add_label` / `remove_label` / `archive` — it has **no delete, trash, or send
  capability at all**. This is a hard, tool-wide guarantee (enforced by an AST
  test), not just a config choice, so a scheduled run cannot destroy mail.
- **A scheduled `apply` MUST include `--yes`.** Without it, `apply` prints
  `Proceed with apply? [y/N]` and waits for input on stdin — which does not exist
  in a scheduled task, so the task **hangs** until it times out. Always pass
  `--yes` in a scheduled command.

Example PowerShell script (`gmail-tidy-scheduled.ps1`) that scans up to 200 new
candidates and applies them if any were found:

```powershell
# gmail-tidy-scheduled.ps1
$ErrorActionPreference = "Stop"
cd C:\path\to\gmail-tidy
.\.venv\Scripts\Activate.ps1

gmail-tidy scan --limit 200
if ($LASTEXITCODE -eq 3) {
    Write-Output "No new candidates; nothing to do."
    exit 0
}
gmail-tidy apply --yes
exit $LASTEXITCODE
```

Register it with Task Scheduler (run as your user, at logon or on a schedule):

```powershell
schtasks /Create /TN "gmail-tidy cleanup" /TR "powershell -ExecutionPolicy Bypass -File C:\path\to\gmail-tidy-scheduled.ps1" /SC DAILY /ST 09:00
```

**Failure behavior to plan for:**

- **Expired Testing-mode token → exit `4` (auth error), no automatic recovery.**
  Because the OAuth consent screen is in Testing status, the token expires after
  7 days and the scheduled run will fail with an auth error until you
  re-authenticate manually (`gmail-tidy auth refresh`). There is no self-healing.
- **Partial failure → exit `6`.** Some batches failed; re-running `apply` resumes
  the incomplete work.
- **Log stdout and the exit code somewhere you will actually check.** Redirect the
  script's output to a file (e.g. append `>> C:\path\to\gmail-tidy.log 2>&1` to the
  `schtasks` command) so a silent failure is visible.

**Do not schedule `undo`.** `undo` is a manual safety-net operation (dry-run by
default; a real write requires `--apply`, and `--apply --yes` writes without a
prompt) — it is not something to automate.

## Documentation

- **[docs/google-cloud-setup.md](docs/google-cloud-setup.md)** — create a Google
  Cloud project, enable the Gmail API, set up the OAuth consent screen, and download
  your `client_secret.json`.
- **[docs/config-reference.md](docs/config-reference.md)** — every `match` and
  `actions` key, the presets, the aliases, the protected-label list, and a full
  worked example.
- **[docs/safety-and-privacy.md](docs/safety-and-privacy.md)** — the eight safety
  invariants, failure/backoff behavior, the undo contract, and the audit-log format.
- **[SECURITY.md](SECURITY.md)** — secrets policy and how to report a vulnerability.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

The suite is **fully offline** — it uses an in-memory `MockGmailApi` double and an
AST test proving no destructive Gmail method is callable. There is currently **no
`--live` option and no live integration harness**: the suite contains no live tests
and never calls Gmail. A future `--live` harness against the real Gmail API (under
`tests/live/`) is **planned but not implemented**. Always run the offline suite
before committing:

- `python -m pytest -q` — all green
- `git ls-files | grep -Ei '(^|/)(token|client_secret).*\.json$'` — no output
