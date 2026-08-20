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
account: you@gmail.com

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

## 6. Safety invariants (test-enforced)

1. The only Gmail write surface is `users.messages.batchModify` (label add/remove) and
   label listing/creation. A greptest fails if `delete|trash|untrash|send|import` is
   referenced anywhere in `src/`.
2. `exclude` is re-evaluated immediately before apply against then-current state; any
   message now excluded, or no longer existing, is skipped.
3. No-op elimination: actions already true are dropped from the plan.
4. Run file records each message's **pre-apply label set** (before-state snapshot) for undo.
5. Audit log (JSONL, `chmod 600`): `{ts, run_id, message_id, thread_id, rule_id, action,
   label}` — never sender/subject/body/size/content.
6. Run files (candidates + checkpoints) are local, `chmod 600`, gitignored.
7. `apply` requires interactive confirmation; `--yes` bypasses, and reprints the diff.

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
- CLI exit-code table via `CliRunner`.
- Greptest for forbidden methods.
- Live testing: explicit `--live` flag only, never in CI, documented in
  `docs/google-cloud-setup.md`.
