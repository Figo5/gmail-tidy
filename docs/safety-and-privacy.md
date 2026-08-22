# Safety & privacy

gmail-tidy is designed so that even a buggy run cannot delete, trash, spam-report,
send, or otherwise destroy mail. This page documents the enforced guarantees, the
failure model, the undo contract, and the minimal-data policy. All invariants are
**test-enforced**, not conventions. See also [SECURITY.md](../SECURITY.md).

## The eight safety invariants

1. **Only the allowed write surface exists.** The sole Gmail write is
   `users.messages.batchModify` (label add/remove, ≤ 1000 ids/batch); labels may be
   listed/created. Enforcement is a **precise AST test**
   (`tests/test_forbidden_api.py`) that parses every Python file in `src/`, not a
   naive grep: only an actual call on a Gmail API resource (e.g.
   `messages().delete(...)`, `threads().delete(...)`, `drafts().send(...)`) fails
   the test. The word "delete" in a docstring or help text never does. The
   `MockGmailApi` double also exposes only the allowed surface, so any test that
   invokes a disallowed method fails fast at runtime.
2. **Reconcile-before-apply.** `exclude` is re-evaluated against **then-current
   state** immediately before each batch write. A message that became excluded, or
   no longer exists, is skipped.
3. **No-op elimination.** Actions already true against the message's current state
   are dropped from the plan.
4. **Pre-apply snapshot.** The run file records each message's pre-apply label set,
   so `undo` can rebuild exactly what the run changed.
5. **Audit-log whitelist.** The audit log stores only `ts`, `run_id`, `message_id`,
   `thread_id`, `rule_id`, `action`, `payload`, `kind` — never
   sender/subject/body/size/content. This is asserted by a test.
6. **Local, private run files.** Run files (candidates + checkpoints) are local,
   `chmod 600`, and gitignored.
7. **Confirmation on writes.** `apply` prompts for confirmation (`Proceed with
   apply? [y/N]`, default No); `--yes` bypasses it. `undo` is **dry-run by default**
   (with no flags it prints the inverse plan and exits `0`); it writes only with
   the new `--apply` flag — `--apply` alone prompts for confirmation
   (`Proceed with undo?`, default No; decline prints `cancelled.` and exits `5`),
   while `--apply --yes` skips the prompt for automation and never reads stdin.
   `--yes` without `--apply` is a usage error (exit `2`), not a silent no-op.
8. **Explicit, complete API surface.** The audited set is
   `users.messages.list/get/batchModify`, `users.labels.list/get/create`, and
   `users.getProfile`. Anything else — `messages.delete/trash/untrash/send/import/
   batchDelete`, `drafts.*`, `threads.delete`, `users.settings.*`,
   `users.watch`/`stop` — is disallowed.

## Failure behavior

- **Rate limits / transient errors (429, 500, 503):** exponential backoff with
  jitter, base 2s, cap 60s, up to 3 attempts per request.
- **Batch as checkpoint unit:** on exhaustion a batch is marked failed in the run
  journal and the next batch proceeds. Re-running `apply` resumes incomplete work.
- **Auth errors:** a `403` (expired/revoked/insufficient scope) prints a clear
  "run `gmail-tidy auth` to re-authenticate" message and exits **4**.
- **Malformed config:** all validation errors are printed, nothing runs, exit **2**.
  Beyond schema checks, config validation rejects dangerous label writes at load
  time: `add_label` may name only **user** labels (any Gmail system label —
  `INBOX`, `UNREAD`, `STARRED`, `IMPORTANT`, `SPAM`, `TRASH`, `DRAFT`, `SENT`,
  `CHAT` — is refused), while `remove_label` may not name a protected label,
  `UNREAD`, or any `Cleanup/*` label. `remove_label: [INBOX]` remains allowed
  because it is the explicit form of archiving.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Runtime error |
| 2 | Config / usage error |
| 3 | Nothing to do |
| 4 | Auth error |
| 5 | Cancelled by user |
| 6 | Partial success (some batches failed; resume with `apply`) |

## Headless `run` command (Task Scheduler)

`gmail-tidy run` is the single-shot scan + apply entry point for unattended
scheduling. It reuses the exact scan/apply/audit/checkpoint engine of the
interactive commands but is deliberately stripped of everything a scheduled
task cannot safely do:

- **It never launches an OAuth browser and never refreshes a token.** Before any
  service is constructed, it checks the cached `token.json`'s persisted scopes
  (`gmail.modify` + `gmail.labels`). A missing, read-only, or unreadable token
  fails with a clear message and exit **4** — `run` never falls through to
  `get_credentials`'s interactive consent path and never deletes your token.
- **It never prompts on stdin.** Candidates are applied through the existing
  `apply_run` with a non-interactive confirmation (always true), so a
  console-less scheduled task cannot hang on `[y/N]`.
- **No automatic self-healing.** A scheduled run that starts failing with exit 4
  stays failing until you run `gmail-tidy auth refresh` once interactively.
- **`--dry-run` exits `0` and never writes.** It scans, journals the plan like
  `scan` does, and stops before the apply phase.
- **No candidates exits `0`** with a `nothing matched` message (not exit 3, so a
  scheduled "nothing to do" is a clean success).
- **Partial failures exit `6`** and are recorded in the run journal exactly as
  with interactive `apply`; a later `run` or `apply` resumes them.
- **Output is aggregate-only.** `run` prints counts, the random run id, and a
  fixed status word (`applied`, `partial`, `dry-run`) — never message ids,
  thread ids, or content. A scheduled run can be logged to a file without
  leaking mailbox identifiers.
- **No new persisted state and no new Gmail surface.** `run` writes the same
  checkpoint, run journal, run stats, and audit log the interactive commands
  write; it never shells out to `schtasks` or any subprocess.

Scheduling itself is always manual (`schtasks` / `Register-ScheduledTask`), run
as your user so the task can read your token and config — see the README's
[Windows Task Scheduler](../README.md#windows-task-scheduler) section.

## Undo contract

The run file records each message's **pre-apply label set**. `undo <run>` rebuilds
the inverse plan: remove labels the run added, re-add labels it removed, and re-add
INBOX for archived messages — **but only if the message's current label set exactly
equals the state the run left behind**.

- Messages the user changed since the run are **skipped, never clobbered** — newer
  user state always wins.
- `undo` is **dry-run by default**; no flags (or `--dry-run`) prints the inverse
  plan and exits `0`. A real write requires `--apply`: `--apply` alone prompts for
  confirmation (decline cancels with exit `5`); `--apply --yes` writes immediately
  with no prompt (never reads stdin, safe for automation). `--yes` without
  `--apply` is a usage error (exit `2`). It is **idempotent**: a second run finds
  the messages already restored and does nothing.
- Undo writes its own audit entries with `kind: "undo"`, keyed to the original
  run_id.

## Audit log

The audit log is an append-only JSONL file at `~/.config/gmail-tidy/audit.jsonl`
(`chmod 600` on POSIX). Each line is one recorded action:

```json
{"ts": 1755700000.123, "run_id": "a1b2c3d4e5f6", "message_id": "18f9f2ab3c", "thread_id": "18f9f2ab3c", "rule_id": "old-unread-newsletters", "action": "add_label", "payload": "Cleanup/Newsletters", "kind": "apply"}
{"ts": 1755700000.456, "run_id": "a1b2c3d4e5f6", "message_id": "18f9f2ab3c", "thread_id": "18f9f2ab3c", "rule_id": "old-unread-newsletters", "action": "archive", "payload": "INBOX", "kind": "apply"}
```

Allowed fields: `ts`, `run_id`, `message_id`, `thread_id`, `rule_id`, `action`,
`payload`, `kind`. No sender, subject, body, size, or content is ever written to
disk by the tool.

## Privacy in fixtures and examples

Test fixtures, mock mailboxes, README examples, and this documentation use **only
synthetic addresses** (`example.com`, `example.org`) — never real senders or real
inbox content. Audit/run files are defined to exclude bodies, subjects, and
senders; screenshots or animations in docs must use mock data labeled "synthetic".

## Live integration tests (`--live`) — planned

Normal usage of every command **intentionally communicates with Gmail** — the
commands are thin clients over the API, and "dry-run" means *no writes*, not *no
network*. The offline unit suite, by contrast, makes **no network calls** at all.

There is currently **no `--live` option and no live integration harness** in the
suite. A future optional `--live` harness under `tests/live/` that would exercise the
real Gmail API against the user's own mailbox is **planned but not implemented**. If
added, it would be:

- **disabled by default**, and never part of normal development or CI;
- run only when explicitly requested, and only after the user has set up their own
  OAuth client (see [docs/google-cloud-setup.md](google-cloud-setup.md));
- excluded by `tests/live/` and `tests/.live/` in `.gitignore`, so live artifacts and
  any real credentials could never be committed.

Running `--live` tests with real credentials and real mail would remain an explicit,
deliberate opt-in.
