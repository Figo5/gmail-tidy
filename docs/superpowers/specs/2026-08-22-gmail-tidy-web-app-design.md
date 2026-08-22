# gmail-tidy Web App — Design Specification (v2 additive, design-only)

Date: 2026-08-22
Status: **Design-only (Task 5 scope). No code, no server, no UI is implemented by this
document.** The companion artifact is the contract-pinning structure test
`tests/test_webapp_contract.py`, which locks this design's data surface to the
actual code (see §12).

## 1. Scope and posture

This document specifies a **v2 additive** feature: a *local-first, read-only,
loopback-only* web viewer for gmail-tidy's existing local state (run files,
audit log, checkpoint, config projection). The viewer renders a single-user
browser view of data that already lives on this machine's disk — it never
contacts Gmail, never holds OAuth credentials, and never writes anything.

**Design-only boundaries for this task:**

- No web server, HTTP handler, or UI code is written in Task 5.
- No `src/` module, CLI command, or test outside this task is modified.
- No dependency is added: `pyproject.toml` is **unchanged**; the future
  implementation must use only the Python standard library plus the existing
  `gmail_tidy` modules (§10).
- No server is run, no browser is opened, no Gmail/OAuth flow is started.
- Tasks 1–6 of the v1 plan are untouched. Only two artifacts are created:
  this design doc and `tests/test_webapp_contract.py`.

## 2. Relationship to v1 — acknowledgements, not edits

Two prior documents are deliberately **not edited**; this design instead
acknowledges and narrows them:

1. **v1 spec non-goal.** `docs/superpowers/specs/2026-08-19-gmail-tidy-design.md`
   §2 lists, for v1: *"No delete/trash/spam/unsubscribe/send. No IMAP. No web
   UI. No scheduler/daemon. …"*. This v2 design is an **additive carve-out**:
   the phrase "No web UI" remains the v1 truth and is **retained for any
   network-exposed, remote, or write-capable UI**. What v2 adds is a narrowly
   scoped exception — a loopback-only, read-only, local viewer. The v1 spec is
   the authority on v1; this doc is the authority on v2's loopback viewer and
   does not rewrite v1. A future multi-user, TLS-terminated, or non-loopback
   UI would be a separate v3 proposal and remains excluded by v1.

2. **Plan numbering collision.** `docs/superpowers/plans/2026-08-19-gmail-tidy.md`
   already numbers a v1 task **"Task 7: Scan and apply (planning +
   reconcile-before-apply)"**. The v2 web-viewer work has colloquially been
   called "Task 7", which collides with the completed v1 Task 7. The v1 plan is
   finished (its Tasks 1–12 are committed) and will **not** be renumbered.
   Going forward, v2 work is addressed by name — *"web viewer design"* (this
   task) and *"web viewer implementation"* (future Task 8, §13) — never by a
   bare "Task 7".

## 3. Goals (v2 additive)

- Readable browser view of scan plans (runs/candidates), audit history, scan
  stats, and checkpoint state — all already persisted locally by v1.
- Aggregate-first privacy posture inherited from v1 (§9): default views are
  counts and per-rule/action groupings; per-message detail views are opt-in
  local routes.
- Zero new risk surface: loopback bind, GET-only, no credentials on or off the
  wire, no new dependencies, no Gmail access of any kind.

## 4. Non-goals (explicit, v2)

- No UI, no server, no module, no CLI flag — **in this task** (Task 5 = design
  + contract tests only).
- No network except the loopback socket. No remote binding, no `--host`, no
  TLS, no reverse-proxy config.
- No write endpoints: the API is **GET-only**. There is no POST/PUT/DELETE, no
  endpoint that triggers `apply`, `undo`, `scan`, `auth`, or any file mutation.
- No Gmail connection, no OAuth, no `GmailClient`, no `googleapiclient`
  anywhere in the web surface. `src/gmail_tidy/web.py` (future) must stay
  Gmail-API-free; the existing AST gate `tests/test_forbidden_api.py` already
  walks every file under `src/`, so it will enforce this automatically.
- No new dependencies and no `pyproject.toml` change (§10).
- No secrets ever: `token.json` and `client_secret*.json` are never read by
  the server and are excluded from the data-source allowlist (§6).
- No authentication layer. Loopback + no-store + GET-only is the design's
  whole security story (§9); a bearer-token scheme is explicitly out of scope.
- No export/download/mirror endpoints; no RSS/email/push; no multi-account
  view; no editing config from the browser.
- v1 non-goals remain in force where not superseded by this carve-out
  (destructive actions, IMAP, daemonization, LLM triage, non-Gmail providers,
  mobile UI, multi-account, remote exposure).

## 5. Architecture overview

```
┌─────────────── browser (user) ───────────────┐
│  static shell (GET /)      │                 │
│  fetch() same-origin GETs  │  127.0.0.1 only │
└──────────────┬─────────────┘                 │
               ▼                               ▼
   ThreadingHTTPServer bound to 127.0.0.1:PORT   (stdlib http.server, Task 8)
        │                                          │
        └── resolves ONLY the fixed route table (§7) through a
            path resolver that maps route → explicit data source.
            No URL-derived filesystem path is ever used.
        ▼
   read-only projections over the config dir:
   config.yaml (projection) · runs/<run_id>.json · runs/<run_id>.stats.json
   audit.jsonl · checkpoint.json · token.json presence+scopes ONLY
```

Invariants:

- **Local-first:** every handler is a pure function of files already on disk.
  Zero network at request time. No Gmail API. No DNS.
- **Read-only:** no handler opens a file for writing; no handler calls a
  `gmail_tidy` function with write side effects. `RunJournal`, `AuditLog`,
  `load_checkpoint`, `scope_state`, and the `render` projections are the only
  imports the web module may use from the project (plus stdlib).
- **Stateless:** each request recomputes its response from the filesystem;
  there is no in-memory cache, no mutation, no session, no cookies.
- **No path traversal surface:** run ids are validated against a strict
  pattern (`^[0-9a-f]{12}$`) before any filesystem access, and even then the
  resolver maps ids to fixed paths, never to caller-supplied strings.

## 6. Data-source allowlist (and explicit exclusions)

Only the following sources may be read by a future web handler. Everything
else in the config dir is **off-limits**.

| Source | Resolved from | Serves |
|---|---|---|
| Run journal | `config_dir()/runs/<run_id>.json` via `RunJournal.load_candidates` | `Candidate` list, serialized exactly as `render.candidate_record` |
| Run stats | `runs/<run_id>.stats.json` via `RunJournal.load_stats` | aggregate scan stats dict |
| Run list | `RunJournal.list_runs()` (chronological; `.stats.json` excluded) | run ids + latest id |
| Audit log | `config_dir()/audit.jsonl` via `AuditLog.entries` | `AuditEntry` fields (see §6b) |
| Checkpoint | `config_dir()/checkpoint.json` via `load_checkpoint(path, cfg)` | per-rule `exhausted`/in-progress **state only — never page tokens** |
| Config | `config_dir()/config.yaml` via `load_config` | projection: rule ids + match criteria (`render.explain_*`-style), never the raw YAML bytes |
| Token | `token_path(config_dir())` **presence check only** (`Path.exists`) | booleans, plus `scope_state()` scopes — never the file's bytes |
| Version | `gmail_tidy.__version__` | version string |

**Explicitly excluded (never read by the server, never served):**

- `token.json` — OAuth access/refresh tokens. Presence may be reported, but the
  file is never opened by a web handler.
- `client_secret.json` / `client_secret*.json` — OAuth client secrets. Never
  opened, never served, never enumerated.
- Any other file under the config dir (e.g. `runs/*.failures.jsonl` values are
  aggregated as counts only, never raw error strings; arbitrary files such as
  config backups, editor swap files) — **not resolvable**; a request that
  names them must yield 404, not content.
- Anything reachable through Gmail (sender, subject, body, thread contents):
  v1 guarantees the run/audit files do not contain them, and the web layer
  adds no new channel to fetch them (no GmailClient, no network).

## 6b. Audit / run-field whitelist

The web layer serializes only fields that already exist in v1's persisted
artifacts — mirroring `render.candidate_record`, which is defined to equal
`RunJournal.save_candidates` output exactly (§12, pin 1). `AuditEntry` fields
are `run_id, message_id, thread_id, rule_id, action, payload, kind, ts` —
sender/subject/body/size/content never appear anywhere in the web output.

## 7. API surface (GET-only)

All responses `Content-Type: application/json` (except `/`, which is the
static HTML shell delivered by Task 8), `Cache-Control: no-store`, no CORS
headers, no cookies. Base path is fixed; every other path is 404. Non-GET
methods are 405.

| GET | Description | Response shape (key fields) |
|---|---|---|
| `/healthz` | liveness | `{"status": "ok"}` |
| `/` | static shell (Task 8) | `text/html` |
| `/api/v1/status` | config-dir summary | `{config_dir, config_present, config_valid, token_present, scopes, checkpoint_present, runs_count, latest_run}` |
| `/api/v1/config` | rules projection | `{rules: [{id, criteria: [...]}]}` (criteria via the pure `explain` projection) |
| `/api/v1/runs` | run list | `{latest: str|null, runs: [str]}` |
| `/api/v1/runs/{run_id}` | full run detail (opt-in) | `{run, candidates: [candidate_record…], stats}` — run_id validated `^[0-9a-f]{12}$` |
| `/api/v1/audit` | audit entries | `{entries: [AuditEntry…]}` with optional `?limit=` (clamped) |
| `/api/v1/audit/summary` | **default view — no message ids** | `{by_rule: {id: count}, by_action: {action: count}, by_kind: {kind: count}}` |
| `/api/v1/checkpoint` | checkpoint summary — **no page tokens** | `{fingerprint, rules: {rule_id: "exhausted"|"in-progress"}}` |
| `/api/v1/health` | alias of `/healthz` | `{"status": "ok"}` |

Detail views (`/api/v1/runs/{id}`, `/api/v1/audit`) intentionally expose
message/thread ids, matching v1's local posture (`preview` prints message ids;
run/audit files are `chmod 600`); aggregate views do not. Both are loopback-only.

Error semantics: 400 (invalid `limit`), 404 (unknown route / unknown run id /
excluded source), 405 (non-GET), 500 (unexpected failure, generic envelope —
no stack traces, no paths, no data in the message).

## 8. CLI seam — `gmail-tidy web [--port N] [--no-browser]`

The command is **declared here and implemented in Task 8** (§13); nothing
changes in `cli.py` now.

```
gmail-tidy web [--port N] [--no-browser]
```

- **`--port N`**: default `8765`; `--port 0` asks the OS for a random free port
  (printed to stdout so `--no-browser` automation can read it).
- **`--no-browser`**: skip the `webbrowser.open` call. Without it, the CLI
  opens the default browser at `http://127.0.0.1:<port>/`.
- Binds **only** `127.0.0.1`. There is no `--host` flag by design.
- Exit codes: `0` clean shutdown (SIGINT/Ctrl-C), `1` (EXIT_RUNTIME) bind or
  startup failure, `2` (EXIT_CONFIG) usage error (e.g. bad `--port`).
- Output hygiene: stdout carries only the URL and aggregate counts; the web
  module logs at most to stderr; no secrets ever printed.
- Reuses the existing console-script entry point `gmail_tidy.cli:app`; no new
  `[project.scripts]` entry, no new dependencies.

## 9. Privacy & threat model

Attackers and the mitigations that this design bakes in:

| Attacker | Capability | Mitigation |
|---|---|---|
| Hostile web page in the user's browser (DNS rebinding / localhost snooping / drive-by fetch) | Can make requests to localhost while the viewer is up | Loopback bind; random ephemeral port is the default for unattended use; **no CORS headers** (`Access-Control-Allow-Origin` never sent → cross-origin JS cannot read responses); `Cache-Control: no-store`; JSON content-type only; run ids validated before any filesystem use; no user-supplied strings interpolated into the HTML shell; GET-only means nothing a page can trigger has side effects |
| Other users / processes on the machine | Read other processes' sockets; read files on disk | Token/client secrets are never read by the server, so a compromised viewer cannot leak them; data exposed is limited to the allowlist and is no broader than what v1 already writes to a `0700`/`0600` config dir; port is not a well-known fixed default for unattended runs (`--port 0`) |
| LAN attacker | Network sniffing | Loopback bind: nothing listens on a routable interface |
| The user themself | — | Detail views opt-in; aggregate defaults mean a quick glance shows no message/thread ids |

**Secret-exclusion rationale (defense in depth):** even on loopback, the
server process never opens `token.json` or `client_secret*.json`. If the
handler is ever compromised or misconfigured (or the machine is shared),
there is nothing credential-shaped to exfiltrate: the max harm is v1's own
local run/audit data. `.gitignore` posture for these files is unchanged.

## 10. Dependency constraints (stdlib-only)

The future implementation must not add or change any dependency in
`pyproject.toml`. Everything used must come from the standard library plus the
existing `gmail_tidy` package:

- stdlib: `http.server` (`BaseHTTPRequestHandler`, `ThreadingHTTPServer`),
  `json`, `urllib.parse`, `pathlib`, `dataclasses`, `webbrowser`, `threading`.
- project modules (existing): `config`, `audit` (`RunJournal`, `AuditLog`,
  `AuditEntry`, `Candidate`), `checkpoint` (`load_checkpoint`,
  `checkpoint_path`), `render` (`candidate_record`, `explain_lines`), `auth`
  (`token_path`, `scope_state`).
- The web module must not import `googleapiclient`, `gmail_client`, `auth`
  beyond the two listed helpers, `actions`, `runner`, `undo`, or anything with
  I/O side effects beyond the allowed reads.

## 11. Test strategy

- This task ships `tests/test_webapp_contract.py`, which pins the **actual**
  contracts the viewer will consume (see next section). It is fully offline:
  imports the real dataclasses, reads only `tmp_path` fixtures and the design
  doc itself — no sockets, no Gmail, no config-dir writes, no network.
- The full suite remains green with exactly one command:
  `PYTHONPATH=src python -m pytest -q`.
- A future `tests/test_webapp.py` (Task 8) will exercise the handlers by
  instantiating them directly in-process against fixture config dirs (no real
  sockets), keeping CI offline.

## 12. This task's deliverable — contract pins

`tests/test_webapp_contract.py` asserts, against the **actual** code (so the
design cannot silently drift from reality):

1. **Candidate / render whitelist** — `Candidate` dataclass field order
   (`message_id, thread_id, rule_id, actions, before_labels, in_inbox`), and
   that `render.candidate_record(candidate)` is byte-for-byte equal to what
   `RunJournal.save_candidates` writes for the same candidate (lockstep).
2. **AuditEntry fields** — exact dataclass field names/order and the exact
   serialized key set; no sender/subject/body/size key may ever appear.
3. **Checkpoint serialized schema** — `ScanCheckpoint`/`RuleCheckpoint` field
   order and the exact JSON keys that `save_checkpoint` writes
   (`config_fingerprint`, `rules` → `page_token`, `exhausted`).
4. **Secret exclusion** — `auth.TOKEN_NAME == "token.json"` and
   `auth.SECRET_NAME == "client_secret.json"`, and the design doc lists both
   names in its exclusion allowlist (§6), so the "never served" promise is
   tied to the real filenames.

## 13. Future Task 8 — implementation seam

The implementer of the web viewer shall:

1. Add `src/gmail_tidy/web.py` (stdlib HTTP server per §3/§9, allowlist per §6,
   routes per §7).
2. Add the `web` command to `src/gmail_tidy/cli.py` exactly as declared in §4
   (typer command `web` with `--port`/`--no-browser`).
3. Add `tests/test_webapp.py` exercising handlers against fixture config dirs.
4. Add no dependencies; leave `pyproject.toml` untouched.
5. Run `PYTHONPATH=src python -m pytest -q`; all tests pass.
6. The v1 AST gate (`tests/test_forbidden_api.py`) covers `web.py` because it
   walks all `src/*.py`; `web.py` must introduce no forbidden Gmail API calls
   (it shouldn't import the Gmail client at all).
7. Do not edit this design's §1–§4 boundaries without a new spec section.
8. `docs/autonomous-progress.md` is updated by the project's own conventions;
   **this task did not modify it**, and neither should Task 8 treat it as a
   staging area for `.claude` artifacts.

## 14. Change log

- 2026-08-22: initial design-only spec (Task 5). No code produced.
