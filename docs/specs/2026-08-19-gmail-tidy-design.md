# gmail-tidy — Design Specification

Date: 2026-08-19
Status: Approved for planning (per user approval of Concept 1 + revised MVP scope)

## 1. Purpose

A privacy-conscious, read-mostly Python CLI that applies declarative cleanup rules to
**existing** Gmail messages using only two write capabilities: **add/remove labels** and
**archive** (remove from INBOX).

Version 1 has **no delete, trash, spam-report, unsubscribe, or send paths anywhere in
the code**. This is an enforced allowlist, not a convention, and is tested.

**Positioning (verified against competitors 2026-08-19, no code copied):**

- `mbrt/gmailctl` (Go, MIT, 2,194★, active): declarative filters for **future** mail
  only; cannot touch existing messages (its own known-issue).
- `sadhgurutech/mailtrim` (Python, MIT, 49★, active): bulk delete-first cleanup with
  trash-based undo; not rule-driven.
- `Gururagavendra/gmail-cleaner` (Python, MIT, 2,066★, active; author on hiatus): web
  GUI bulk cleanup; no AI despite the name.
- `jerus-org/cull-gmail` (Rust, MIT, 2★, active): rule-based retention with dry-run
  defaults, but supports permanent delete.
- `gmail-llm-cleanup`: no exact repo; nearest (chrisdruta/local-llm-email-cleaner,
  vpapats/gmail-cleanup-agent) are zero-star experiments.

**Gap:** no tool combines *declarative rules + existing-mail application + zero-
destructive actions + global include/exclude protection*. gmail-tidy occupies it.

## 2. Non-goals (v1)

No delete/trash/spam/unsubscribe/send. No IMAP. No web UI. No scheduler/daemon. No LLM
triage. No multi-account management (single OAuth context). No running against non-Gmail
providers. No mobile UI.

## 3. Architecture

**Plan vs. play separation:**

- `scan` / `preview` build a *plan* from pure functions. They make read-only Gmail calls
  and write a local run file. No write operations are ever issued from these commands.
- `apply` is the only command that mutates Gmail, and it re-verifies every batch
  immediately before writing.

**OAuth scope model (amended):**

- **Read-only commands** — `scan`, `preview`, `status`, and `auth status` — require only
  the `gmail.readonly` scope. `init` establishes this scope. No write capability is held
  during these commands, so a misbehaving run of `scan`/`preview` cannot mutate anything.
- **Write commands** — `apply` and `undo` — require the escalated scope combination
  `https://www.googleapis.com/auth/gmail.modify` (which permits `batchModify` label
  changes, including adding/removing INBOX) **plus** `https://www.googleapis.com/auth/gmail.labels`
  (label creation). `gmail.modify` alone is never sufficient to permanently delete,
  trash, or send; those methods are additionally blocked by the API-surface test (§6.1).
- **Scope escalation & reauthentication:** the token file records which scopes it was
  minted with. When `apply` or `undo` runs and the stored token lacks the write scopes,
  the CLI prompts the user, performs a **new interactive OAuth consent flow** (the
  Google consent screen explicitly lists the broader scopes), and persists a new token
  that supersedes the old one. `scan`/`preview` after an escalation continue to use the
  (now broader) token — the token is a single credential, not per-command. A token
  revoked at the Google end, or a `403` scope/expiry error, triggers re-consent with a
  clear message ("run `gmail-tidy auth` to re-authenticate"), exit 4. Users who want
  read-only-only usage can force a fresh read-only token with `gmail-tidy auth revoke`
  then `init`.
- **`auth revoke`:** deletes the local token file (`token.json` in the config dir,
  `chmod 600`) after best-effort revocation via the token-info/revoke endpoint; if the
  network call fails, the local file is still removed and the user is told the token
  remains valid server-side until it expires. Safe to call at any time; subsequent
  commands simply re-prompt for consent. `auth revoke` never deletes the config or
  audit log.

**Data flow:**

```
config.yaml ──► RuleEvaluator (pure) ──► CandidateSet (metadata only)
                                             │
   Gmail API (list + get metadata,           │  reconcile: re-check exclude +
        paged, retried) ◄────────────────────┘  current state before write
                                             ▼
                      plan (run file, chmod 600, local)
                                             │
   apply ──► batchModify (≤1000 ids/batch) ──► journal checkpoint ──► audit log (JSONL)
```

**Rule evaluation is local:** Gmail search (`q=`) is used to *narrow* the candidate set
only. Eligibility is decided locally from fetched metadata (labels, headers, size,
internalDate), never by trusting the search result.

## 4. Command surface & exit codes

| Command | Behavior |
|---|---|
| `init` | Create config dir (`~/.config/gmail-tidy/`), write commented template with presets **disabled**, start OAuth with `gmail.readonly` |
| `scan [--limit N] [--rules ID…]` | Build candidate plan → local run file; prints counts only |
| `preview [--run ID]` | Render full proposed-action table (sender, subject, size, action, rule); dry-run by default |
| `apply [--run ID] [--yes]` | Re-verify → confirm → execute in batches → journal → audit log |
| `undo <run ID> [--dry-run] [--yes]` | Reverse the run's actions from the run file; idempotent (§10) |
| `status` | Account, scopes held, last run, run history, audit-log path |
| `auth` | `status` / `refresh` / `revoke`; upgrades `readonly` → `modify + labels` only when a write action is actually about to be applied |

Exit codes: `0` success · `1` runtime error · `2` config/usage · `3` nothing to do ·
`4` auth error · `5` cancelled by user · `6` partial success (some batches failed;
resume with `apply`).

## 5. Configuration schema (YAML)

At `~/.config/gmail-tidy/config.yaml`; override `GMAIL_TIDY_CONFIG`. Init writes a
commented template; `account` is optional. Preset categories ship present but **disabled
by default** — a fresh install's `scan` changes nothing.

```yaml
account: you@example.com

protect:
  include: []            # if non-empty, must match ≥1 to be eligible
  exclude:               # matching ANY excludes → never touched
    - match_from: ["bank@example.com", "boss@example.com"]
    - match_label: ["IMPORTANT", "STARRED", "Work"]

rules:
  - id: "old-unread-newsletters"
    match:
      category: newsletters
      older_than_days: 30
      unread: true
    actions:
      add_label: ["Cleanup/Newsletters"]
      archive: true
```

**Match grammar** (metadata-only, AND within one `match`, `match_any` for OR groups):
`category` (preset), `from_contains`, `from_ends`, `to_contains`, `subject_contains`,
`labels_have`, `labels_missing`, `older_than_days`, `newer_than_days`, `larger_than_kb`,
`unread`, `query` (raw Gmail search string, narrowing only).

**Default category presets** (shipped commented-out; heuristic from-senders + Gmail
`category:` queries for narrowing only): `newsletters`, `promotions`, `receipts`,
`notifications`, `old_unread`, `large_messages`.

**Actions:** `add_label[]`, `remove_label[]`, `archive` (bool).

**`remove_label` behavior (precise):** `remove_label` removes only the explicitly
listed label names from the message. **Protected labels are never removed or modified
under any circumstance** — `protect.exclude` is a set of labels matched by
`match_label`, and any message matching an exclude is already ineligible (§6.2). Beyond
that, the system maintains a **never-touch label set** — `IMPORTANT`, `STARRED`,
`SPAM`, `TRASH`, `DRAFT`, `SENT`, `CHAT`, and the top-level `Cleanup/…` labels created
by this tool — that no `remove_label` (nor any other action) may act on. A rule that
names a protected label in `remove_label` fails config validation at load time
(exit 2, with the offending rule id), never at apply time. Because a message must first
pass the include/exclude gate to appear in any plan, an excluded message can never be
modified; the reconcile step before apply re-checks this (§6.2).

## 6. Safety invariants (test-enforced)

1. The only Gmail write surface is `users.messages.batchModify` (label add/remove) and
   label listing/creation. Enforcement is a **precise AST / API-surface test**, not a
   naive grep. `test_forbidden_api.py` parses every Python file in `src/` with `ast`,
   walks all attribute-access chains, and fails only if the code references an actual
   disallowed Gmail API resource/method pair — e.g. `messages().delete(...)`,
   `messages().trash(...)`, `messages().untrash(...)`, `messages().send(...)`,
   `messages().import_(...)`, `messages().batchDelete(...)`, or any `users().spam`,
   `threads().delete(...)`, `users().stop`/`drafts().send` call. Plain words like
   "delete" in docstrings, help text, error messages, variable names, or comments do
   **not** trigger a failure; only a callable method on a Gmail API resource object
   does. The test parses function calls and their `resource.method` receivers — a
   local function named `delete()` or a string constant "trash" is ignored. The mock
   (`MockGmailApi`) also exposes **only** the allowed surface, so any test that tries
   to invoke a disallowed method fails fast at runtime.
2. `exclude` is re-evaluated immediately before apply against then-current state; any
   message now excluded, or no longer existing, is skipped.
3. No-op elimination: actions already true are dropped from the plan.
4. Run file records each message's **pre-apply label set** (before-state snapshot) for undo.
5. Audit log (JSONL, `chmod 600`): `{ts, run_id, message_id, thread_id, rule_id, action,
   label}` — never sender/subject/body/size/content.
6. Run files (candidates + checkpoints) are local, `chmod 600`, gitignored.
7. `apply` requires full confirmation; `--yes` bypasses, and reprints the diff.

8. **Allowed API surface is explicit (amended):** the complete, audited set of Gmail
   API methods the code may call is `users.messages.list` (search/metadata),
   `users.messages.get` (metadata only, `format=metadata`), `users.messages.batchModify`
   (label add/remove — the sole write), `users.labels.list`, `users.labels.get`,
   `users.labels.create`, and `users.getProfile` (for `status`/account). Anything else —
   `messages.delete/trash/untrash/send/import/batchDelete`, `batchModify` invoked with
   anything other than label add/remove ids, `drafts.*`, `threads.delete`,
   `users.settings.*` — is disallowed by the API-surface test (§6.1).
   `batchModify` accepts up to 1000 message ids per call and a `addLabelIds`/`removeLabelIds`
   payload; batching never exceeds this (amended: architecture diagram and tests both
   use the 1000 limit).

## 7. Failure behavior

- 429/500 → exponential backoff + jitter (base 2s, cap 60s; respect `Retry-After`).
- Batch retries capped; on exhaustion the batch is marked failed in the journal and the
  next batch proceeds. **Batches are the checkpoint unit** → re-run resumes incomplete.
- 403 auth expiry → clear `gmail-tidy auth` message, exit 4.
- Malformed config → print all validation errors, exit 2, nothing runs.

## 8. Repository structure

```
gmail-tidy/
  pyproject.toml  README.md  LICENSE  .gitignore
  .github/workflows/ci.yml
  src/gmail_tidy/
    cli.py        # typer commands → exit codes
    config.py     # YAML load + Pydantic validation + presets
    rules.py      # RuleEvaluator (pure: match → actions)
    actions.py    # planning, no-op elimination, allowlist, reconcile-before-apply
    gmail_client.py  # paged wrapper (list/metadata/batchModify), retries
    auth.py       # OAuth2 InstalledAppFlow, scope escalation, token mgmt
    audit.py      # JSONL audit + run journal (checkpoints, resume)
    errors.py     # exceptions → exit codes
  tests/
    conftest.py   # MockGmailApi fixtures
    test_config.py test_rules.py test_actions.py test_gmail.py
    test_audit.py test_undo.py test_cli.py test_forbidden_api.py
  docs/
    google-cloud-setup.md  config-reference.md  safety-and-privacy.md
```

## 9. Dependencies & rationale

| Dep | Why |
|---|---|
| Python ≥3.11 | target LTS-ish, modern typing |
| typer + rich | CLI ergonomics + readable tables |
| PyYAML + pydantic | config parsing + validation |
| google-auth-oauthlib, google-api-python-client | OAuth2 InstalledAppFlow + Gmail API |
| pytest + responses (or manual stub) | offline mocking |

## 10. Undo

The run file records each message's **pre-apply label set**. `undo` rebuilds the
inverse plan: remove labels the run added, re-add labels it removed, and re-add INBOX
for archived messages — **only if the message's current state still matches what the
run left behind** (reconcile per batch). Messages changed since are skipped, never
clobbered. `undo` is dry-run by default, idempotent, and its inverse plan can be
previewed. Undo writes its own audit entries (run_id = original run, kind=undo).

## 11. Testing (fully offline)

- `MockGmailApi` fixture: in-memory mailbox, pagination, labels, injected failures.
- Evaluator matrix, include/exclude precedence, no-op elimination, before/after diffs.
- Pagination exhaustion, batch splitting at 1000, backoff/retry, error mapping.
- Journal checkpoint/resume; undo reconstruction + idempotence.
- **Undo safety test (amended):** a test mutates a message *after* the mock records the
  run's "left-behind" state, then runs `undo`; the assertion is that the newer user
  change (e.g. a label the user added, or a message the user re-archived) is **left
  untouched and the message is skipped**, never overwritten by the inverse plan.
- CLI exit-code table via `CliRunner`.
- **API-surface AST test** for forbidden Gmail methods (see §6.1; precisely scoped, not a
  naive grep).
- **`--live` is for optional integration testing only** (amended): the `--live` flag
  gates a small, separately-invoked integration harness under `tests/live/` that
  exercises the real Gmail API against the user's own mailbox. It is never part of the
  normal `scan`/`preview`/`apply`/`undo` flow. Normal usage of every command
  **intentionally communicates with Gmail** (they are thin clients over the API) —
  "dry-run" in this project means *no writes*, not *no network*. The offline unit suite
  and CI never make a network call; `--live` tests are excluded by default and run only
  when explicitly requested.

## 12. GitHub security guidance (public repo, amended)

Published at `SECURITY.md` and `docs/safety-and-privacy.md`:

- **Google Cloud OAuth setup:** users create their own OAuth Client ID in the Google
  Cloud Console (Desktop app type), with scopes `gmail.readonly` for scan/preview and
  `gmail.modify` + `gmail.labels` for apply/undo. The consent screen is "Internal" if
  the project is personal, else "Testing" with test users; production users publish the
  app and self-verify scopes. Step-by-step instructions live in `docs/google-cloud-setup.md`.
- **Client secret files:** `client_secret.json` is downloaded by the user from the
  console, placed in the config dir, never hardcoded, never committed, and matched in
  `.gitignore`. The CLI never embeds or ships a default secret.
- **Token storage:** OAuth tokens are stored in the config dir (`token.json`) with
  `chmod 600` and never committed; on POSIX the config dir is `0700`; tokens are scoped,
  refreshed, and revocable via `gmail-tidy auth revoke`.
- **`.gitignore`:** the repo's `.gitignore` always contains `client_secret*.json`,
  `token.json`, `*.local`, config dirs, and any generated OAuth/credential files, and a
  `docs/` note tells contributors to extend it for their own secrets.
- **Secret scanning:** enable GitHub secret scanning; never let generated OAuth files,
  tokens, or API keys reach the repo. A CI check fails if `token.json`/`client_secret*.json`
  patterns appear in the tree.
- **No personal data in fixtures, logs, screenshots, or examples:** unit fixtures and
  README/doc examples use only synthetic addresses (`example.com`, `example@example.org`),
  never real senders or real inbox content; the audit log and run files are defined to
  exclude bodies/subjects/senders (§6.5, §6.6); screenshots/animations in docs must use
  mock data with a visible "synthetic" note.
