# gmail-tidy web viewer

A **local-first, loopback-only, read-only** browser view of the data gmail-tidy
already keeps on this machine. It renders run files, the audit log, checkpoint
state, and a config projection — it never contacts Gmail, never holds OAuth
credentials, and never writes anything. Data stays on this machine.

The viewer is the v2 carve-out documented in
[docs/superpowers/specs/2026-08-22-gmail-tidy-web-app-design.md](superpowers/specs/2026-08-22-gmail-tidy-web-app-design.md).

## Launch

```bash
gmail-tidy web [--port N] [--no-browser]
```

- **`--port N`** — bind on `127.0.0.1` port `N`. Default `8765`, range `0`–`65535`.
- **`--port 0`** — ask the OS for a random free port. The chosen port is printed
  to stdout (`http://127.0.0.1:<port>/`), so automation with `--no-browser` can
  read it.
- **`--no-browser`** — skip the `webbrowser.open` call. Without it, the CLI opens
  the default browser at `http://127.0.0.1:<port>/`.
- The server binds **strictly `127.0.0.1`** — there is deliberately no `--host` flag, no TLS, no reverse-proxy configuration. Every request whose `Host` header does not name a loopback host is rejected with a generic `403` before any route or data access.

**Exit codes:**

- `0` clean shutdown (Ctrl-C)
- `1` bind or startup failure
- `2` usage error (e.g. a `--port` outside `0`–`65535`)

## Views

All eight views, in navigation order. Hash routing is client-only: an unknown
hash falls back to **overview**.

| View | Route | Description |
|---|---|---|
| `overview` | `#/overview` | Default view. Cards for runs, config validity, token presence, checkpoint; checkpoint and audit summaries; recent runs |
| `runs` | `#/runs` | List of scan run ids, newest first |
| `run` | `#/run/<run_id>` | Detail for one run: scan stats and candidates (message/thread ids, rule, actions, before-labels, inbox flag). The run id is validated against `^[0-9a-f]{12}$` |
| `audit` | `#/audit` | Recent audit entries (up to `?limit=200`, clamped to 500) |
| `rules` | `#/rules` | Configured rules, **criteria only** — never actions or raw YAML |
| `checkpoint` | `#/checkpoint` | Per-rule scan state (`exhausted` / `in-progress`) and fingerprint — **never page tokens** |
| `setup` | `#/setup` | The CLI commands that change things (the viewer itself is read-only) |
| `privacy` | `#/privacy` | The viewer's privacy posture in plain words |

## API routes

The server resolves exactly these routes (everything else is `404`; non-GET
methods are `405`). All responses are `Cache-Control: no-store`, and the API
sends **no CORS headers** and sets no cookies.

| GET | Description |
|---|---|
| `/` | The static HTML shell (the single page that renders all views) |
| `/healthz` | Liveness: `{"status": "ok"}` |
| `/api/v1/health` | Alias of `/healthz` |
| `/api/v1/status` | Config-dir summary: config presence/validity, token presence, scopes, checkpoint presence, run count, latest run |
| `/api/v1/config` | Rules projection: `{rules: [{id, criteria}]}` — match criteria only, never the raw YAML bytes |
| `/api/v1/runs` | Run list: `{latest, runs}` |
| `/api/v1/runs/{run_id}` | One run's candidates + stats (`run_id` validated `^[0-9a-f]{12}$`) |
| `/api/v1/audit` | Audit entries with optional `?limit=` (default `200`, clamped to `500`) |
| `/api/v1/audit/summary` | Default aggregate view — counts by rule/action/kind, **no message ids** |
| `/api/v1/checkpoint` | Per-rule `exhausted` / `in-progress` status + fingerprint — **never page tokens** |

The client shell consumes `/api/v1/status`, `/api/v1/config`, `/api/v1/runs`,
`/api/v1/runs/{run_id}`, `/api/v1/audit/summary`, `/api/v1/audit?limit=200`, and
`/api/v1/checkpoint`.

## Security and privacy posture

- **Loopback-only:** binds strictly `127.0.0.1`; the transport rejects any
  foreign `Host` header with a generic `403` (no-store) before any route or data
  access.
- **GET-only, read-only:** the viewer cannot change anything. Every mutation —
  `init`, `scan`, `apply`, `undo`, `auth refresh`, config edits — happens through
  the **CLI**.
- **No CORS headers:** `Access-Control-Allow-Origin` is never sent, so hostile
  web pages in your browser cannot read responses.
- **No cookies, no sessions, no tracking:** every request recomputes its response
  from disk.
- **`Cache-Control: no-store`** on every response.
- **Secrets are never read:** `token.json` contributes presence and scope names
  only — its bytes (access/refresh tokens) are never opened. `client_secret*.json`
  is never read and never served.
- **Minimal data:** aggregate views show counts only; detail views show only
  existing local run/audit ids and action names. Message bodies, senders, and
  subjects are never fetched, stored, or shown, and checkpoint **page tokens** are
  never displayed.

## Example (synthetic only)

No real addresses appear in this viewer's logs or docs; every example in this
repository uses only synthetic addresses (`example.com`). A candidate in the
runs view looks like a message id, thread id, rule id, and an action such as
`+label: Cleanup/Newsletters; archive` — the data that gmail-tidy already keeps
locally, nothing more.
