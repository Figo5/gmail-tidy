# Security Policy

## Reporting a vulnerability

Please report security issues privately to the maintainers rather than in a public
issue. Open a **private** GitHub security advisory under *Report a vulnerability* on
this repository (Settings → Security policy / Security advisories → *New advisory*),
or file an issue and mention that it is security-sensitive so it can be triaged
privately. Include:

- the affected version(s) and commit hash if known;
- a minimal reproduction;
- the impact you believe the issue has.

Acknowledgment and triage is handled by the maintainers; do not disclose the issue
publicly until a fix has shipped.

## Allowed write surface

gmail-tidy's only Gmail write capability is `users.messages.batchModify`
(label add/remove, ≤ 1000 ids/batch), plus `users.labels.list/get/create`. The
complete audited API surface is:

`users.messages.list` · `users.messages.get` (metadata only) ·
`users.messages.batchModify` (the sole write) · `users.labels.list` ·
`users.labels.get` · `users.labels.create` · `users.getProfile`.

Everything else — delete, trash, untrash, send, import, batch-delete, spam reporting,
`drafts.*`, `threads.delete`, `users.settings.*`, `users.watch`/`stop` — is
**disallowed and absent from the code**. Two independent gates enforce this:

1. An **AST-level test** (`tests/test_forbidden_api.py`) parses every Python file in
   `src/` and fails on any actual Gmail resource/method call outside the allowlist.
   Plain words like "delete" in comments or help text never trigger it.
2. The **`MockGmailApi` test double** exposes only the allowed surface, so a test that
   invokes anything else fails fast at runtime.

Protected labels (`IMPORTANT`, `STARRED`, `SPAM`, `TRASH`, `DRAFT`, `SENT`, `CHAT`,
and tool-created `Cleanup/*`) can never be removed or modified — naming one in
`remove_label` is a config-load error (exit 2).

## No personal data policy

- Test fixtures, mock mailboxes, README examples, and docs use **only synthetic
  addresses** (`example.com`, `example.org`) — never real senders or real inbox
  content.
- The audit log and run files are defined to store **only** IDs and label operations:
  `ts`, `run_id`, `message_id`, `thread_id`, `rule_id`, `action`, `payload`, `kind`.
  Senders, subjects, bodies, sizes, and content are never written to disk by the tool.
- Screenshots or animations in documentation must use mock data labeled "synthetic".

## Secrets policy (public repo)

This repository is public and is designed to be safe to publish as-is. Please help
keep it that way:

- **`client_secret*.json`** (your Google OAuth client secret) is **never committed**.
  It is downloaded by each user from the Google Cloud Console into their config dir,
  matched by `.gitignore`, and never hardcoded in code.
- **`token.json`** (your OAuth access/refresh token) is **never committed** and
  matched by `.gitignore`. On POSIX it is stored with `chmod 600` inside a `0700`
  config dir.
- **Never commit** `token.json`, `client_secret*.json`, `*.local`, generated
  OAuth/credential files, personal data, or generated artifacts (run files, audit
  logs, coverage output). If a credential ever lands in the tree, rotate/revoke it
  immediately — do not rely on deletion to make it private.
- **Secret scanning** is enabled on this repository (GitHub secret scanning, push
  protection). A **CI check** also fails if `token.json` / `client_secret*.json`
  patterns appear in the tree.
- **Extend `.gitignore`** in your own forks/working copies for any additional secrets
  your environment produces. Never weaken the existing `client_secret*.json`,
  `token.json`, or `tests/live/` / `tests/.live/` entries.

## OAuth setup and scope hygiene

Each user creates their **own** OAuth client in the Google Cloud Console — the CLI
ships no client secret and no defaults. See
[docs/google-cloud-setup.md](docs/google-cloud-setup.md) for step-by-step setup.

- **Read-only by default:** `scan`, `preview`, `status`, and `auth status` hold only
  the `gmail.readonly` scope. `init` establishes this scope.
- **Write scope only when needed:** `apply` and `undo` require `gmail.modify` +
  `gmail.labels`, obtained through a **new interactive consent** whose consent screen
  explicitly lists the broader scopes. A token lacking the required scopes is never
  silently reused (scope sufficiency is checked against the persisted token, not
  assumed).
- **Revoke:** `gmail-tidy auth revoke` best-effort revokes server-side, always removes
  the local `token.json`, and never touches your config or audit log.
- A `403` from Gmail (expired/revoked/insufficient scope) maps to a clear
  `gmail-tidy auth` message and exit 4.

## Live integration tests (`--live`) — planned

There is currently **no `--live` option and no live integration harness**: the test
suite is offline and makes **no network calls**. A future optional `--live` harness
under `tests/live/` that would exercise the real Gmail API against the user's **own**
mailbox is **planned but not implemented**. If added, it would be **disabled by
default**, excluded from CI, and never part of normal development. Running `--live`
tests with real credentials and real mail would remain an explicit, deliberate
opt-in — never a default.
