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

```bash
python -m pip install gmail-tidy
```

Requires Python 3.11+. The package uses `google-auth-oauthlib` +
`google-api-python-client` for OAuth2, `typer`/`rich` for the CLI, and
`pydantic`/`PyYAML` for config validation.

## Quick start

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
   run file.
5. **Preview:** `gmail-tidy preview` renders the proposed actions (dry-run, no
   writes).
6. **Apply:** `gmail-tidy apply --yes` re-verifies every message against current
   state, then executes. Scope escalates to `gmail.modify` + `gmail.labels` only at
   this point, via a fresh consent prompt.
7. **Undo:** `gmail-tidy undo <run>` reverses the run — dry-run by default,
   idempotent, and it **skips messages the user changed since** (see
   [docs/safety-and-privacy.md](docs/safety-and-privacy.md)).

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

## Safety model

- **Plan vs. play:** `scan`/`preview` never write. `apply` is the only write command
  and re-checks `exclude` against live state immediately before each batch.
- **No destructive surface:** delete / trash / spam-report / send / import are absent
  from the code and enforced by an AST test over `src/`, plus a mock that exposes only
  the allowed Gmail surface.
- **Protected labels are never touched:** `IMPORTANT`, `STARRED`, `SPAM`, `TRASH`,
  `DRAFT`, `SENT`, `CHAT`, and tool-created `Cleanup/*` labels can never be removed
  or modified; a rule that names one fails config validation (exit 2).
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
| `undo <run ID> [--dry-run] [--yes]` | Reverse a run's actions from its before-state snapshot; idempotent |
| `status` | Account, scopes held, last run, run history, audit-log path |
| `auth status` / `auth refresh` / `auth revoke` | Inspect / escalate (read-only → `modify` + `labels`) / revoke tokens |

**Exit codes:** `0` success · `1` runtime error · `2` config/usage · `3` nothing to
do · `4` auth error · `5` cancelled by user · `6` partial success (some batches
failed; resume with `apply`).

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
