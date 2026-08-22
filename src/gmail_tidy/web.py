"""Loopback-only, read-only web viewer over gmail-tidy's local state (Task 8).

This module is the implementation seam declared by the v2 design spec
``docs/superpowers/specs/2026-08-22-gmail-tidy-web-app-design.md``. It serves a
single-user browser view of data that already lives on disk:

- stdlib only (``http.server``, ``urllib.parse``, ``json``, ``webbrowser``);
- binds strictly ``127.0.0.1`` — never a routable interface;
- the transport rejects any request whose ``Host`` header does not name a
  loopback host (``127.0.0.1`` / ``localhost`` / ``::1``, with optional port
  or brackets) with a generic 403 before any route or data access;
- GET-only, fixed route table, no CORS headers, no cookies, ``Cache-Control:
  no-store`` on every response;
- reads ONLY the allowlisted local files/functions (§6 of the design):
  ``config.yaml`` (projection), ``runs/*.json``/``runs/*.stats.json``,
  ``audit.jsonl``, ``checkpoint.json`` (state only — never page tokens),
  and ``token.json`` presence + scopes ONLY (only the ``scopes`` field is
  read for display names; credential bytes — access/refresh tokens — are
  never surfaced, and ``client_secret*.json`` is never read, never served);
- never writes, never contacts Gmail, never imports the Gmail client,
  actions, runner, undo, rules, or labels, and has no authentication layer
  (bearer tokens are explicitly out of scope per the design §4).

The request surface is a pure seam: ``handle(method, path, cfg_dir) -> Response``
so every route is testable in-process with no sockets.
"""

from __future__ import annotations

import json
import re
import sys
import webbrowser
from dataclasses import asdict, dataclass, field, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from gmail_tidy import __version__
from gmail_tidy import auth as auth_mod
from gmail_tidy.audit import AuditEntry, RunJournal
from gmail_tidy.checkpoint import checkpoint_path
from gmail_tidy.config import ConfigError, MatchConfig, config_dir, load_config
from gmail_tidy.errors import EXIT_OK, EXIT_RUNTIME
from gmail_tidy.render import candidate_record

# Fixed, strictly-validated route prefix for run detail views. Run ids are
# 12 lowercase hex chars (uuid4().hex[:12]) as written by RunJournal.init_run.
# The id is validated BEFORE any filesystem access; it is only ever spliced
# into a fixed path, never used as a caller-supplied string.
RUN_ID_RE = re.compile(r"^[0-9a-f]{12}$")

# Clamp for the audit route's ?limit= parameter.
AUDIT_DEFAULT_LIMIT = 200
AUDIT_MAX_LIMIT = 500


class _RouteError(Exception):
    """Internal: a route-level, user-visible error. Never leaks paths.

    status: 400 for invalid client input (e.g. ?limit=), 404 for unknown
    routes / unknown run ids / excluded sources.
    """

    def __init__(self, message: str, status: int = 404):
        super().__init__(message)
        self.status = status


@dataclass
class Response:
    """Immutable HTTP response produced by the pure handle() seam."""

    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    extra_headers: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure projections (read-only, no write side effects, allowlisted sources)
# ---------------------------------------------------------------------------


def _status_projection(cfg_dir: Path) -> dict:
    conf = cfg_dir / "config.yaml"
    config_present = conf.exists()
    config_valid = config_present
    if config_present:
        try:
            load_config(conf)
        except ConfigError:
            config_valid = False
    token = auth_mod.token_path(cfg_dir)
    token_present = token.exists()
    # scope_state() reads ONLY the scopes field of the token for display names;
    # the token's bytes (credentials, refresh token) are never surfaced.
    scopes = sorted(auth_mod.scope_state(token))
    checkpoint_present = checkpoint_path(cfg_dir).exists()
    runs = RunJournal(cfg_dir / "runs").list_runs()
    return {
        "config_dir": str(cfg_dir),
        "config_present": config_present,
        "config_valid": config_valid,
        "token_present": token_present,
        "scopes": scopes,
        "checkpoint_present": checkpoint_present,
        "runs_count": len(runs),
        "latest_run": runs[-1] if runs else None,
    }


def _match_criteria(match: MatchConfig) -> list[dict]:
    """Structured, non-empty match criteria in stable dataclass field order.

    Mirrors render.explain_lines' field order and emptiness rules, but emits
    structured values (not prose lines). Never includes actions or raw YAML.
    """
    out: list[dict] = []
    for fld in fields(MatchConfig):
        value = getattr(match, fld.name)
        if value is None or value == []:
            continue
        out.append({"name": fld.name, "value": value})
    return out


def _config_projection(cfg_dir: Path) -> dict | None:
    conf = cfg_dir / "config.yaml"
    if not conf.exists():
        return None
    try:
        cfg = load_config(conf)
    except ConfigError:
        return None
    return {
        "rules": [
            {"id": rule.id, "criteria": _match_criteria(rule.match)}
            for rule in cfg.rules
        ]
    }


def _runs_projection(cfg_dir: Path) -> dict:
    runs = RunJournal(cfg_dir / "runs").list_runs()
    return {"latest": runs[-1] if runs else None, "runs": runs}


def _run_projection(cfg_dir: Path, run_id: str) -> dict:
    journal = RunJournal(cfg_dir / "runs")
    try:
        candidates = journal.load_candidates(run_id)
    except FileNotFoundError:
        raise _RouteError("not found")
    stats = journal.load_stats(run_id)
    return {
        "run": run_id,
        "candidates": [candidate_record(c) for c in candidates],
        "stats": stats,
    }


def _read_audit_entries(cfg_dir: Path) -> list[dict]:
    """Read audit.jsonl directly into the pinned AuditEntry schema.

    AuditLog.__init__ calls path.touch() (a write), which the web layer must
    never do; reading the allowlisted file ourselves is a pure read and keeps
    the exact on-disk schema pinned by tests/test_webapp_contract.py.
    """
    path = cfg_dir / "audit.jsonl"
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(asdict(AuditEntry(**json.loads(line))))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue  # defensive: never let a corrupt line break the endpoint
    return entries


def _audit_projection(cfg_dir: Path, query: str) -> dict:
    limit = AUDIT_DEFAULT_LIMIT
    raw_limit = parse_qs(query).get("limit")
    if raw_limit:
        raw = raw_limit[-1]
        try:
            limit = int(raw)
        except ValueError:
            raise _RouteError("invalid limit", status=400)
        if limit < 1:
            raise _RouteError("invalid limit", status=400)
        limit = min(limit, AUDIT_MAX_LIMIT)
    entries = _read_audit_entries(cfg_dir)
    return {"entries": entries[-limit:]}


def _audit_summary_projection(cfg_dir: Path) -> dict:
    by_rule: dict[str, int] = {}
    by_action: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for entry in _read_audit_entries(cfg_dir):
        by_rule[entry["rule_id"]] = by_rule.get(entry["rule_id"], 0) + 1
        by_action[entry["action"]] = by_action.get(entry["action"], 0) + 1
        by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
    return {"by_rule": by_rule, "by_action": by_action, "by_kind": by_kind}


def _checkpoint_projection(cfg_dir: Path) -> dict:
    """Fingerprint + per-rule status only — NEVER page tokens.

    Reads the allowlisted checkpoint.json directly. If it is missing or
    corrupt, an empty state is returned rather than an error: a stale viewer
    must degrade gracefully. The stored config_fingerprint is surfaced as-is
    (it is a sha256 hash, not a secret). page_token is deliberately discarded.
    """
    path = checkpoint_path(cfg_dir)
    fp: str | None = None
    rules: dict[str, str] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            fp = data.get("config_fingerprint")
            for rid, r in data.get("rules", {}).items():
                rules[str(rid)] = (
                    "exhausted" if r.get("exhausted") else "in-progress"
                )
        except (OSError, json.JSONDecodeError, AttributeError):
            fp, rules = None, {}
    return {"fingerprint": fp, "rules": rules}


# ---------------------------------------------------------------------------
# Route resolution
# ---------------------------------------------------------------------------

_RUN_DETAIL_RE = re.compile(r"^/api/v1/runs/([0-9a-f]{12})$")

_SHELL = None


def _html_shell() -> bytes:
    """Static inline HTML shell (Task 8). No user input is interpolated."""
    global _SHELL
    if _SHELL is None:
        _SHELL = _SHELL_TEXT.encode("utf-8")
    return _SHELL


def handle(method: str, path: str, cfg_dir: Path | str) -> Response:
    """Pure request seam: (method, path, cfg_dir) -> Response. No sockets, no writes.

    - unknown routes, unknown/excluded sources, and invalid run ids -> 404
    - non-GET -> 405
    - invalid ?limit= -> 400
    - any unexpected failure -> 500 with a generic envelope (no paths, no
      tracebacks, no data)
    """
    split = urlsplit(path)
    pathname = split.path
    if method != "GET":
        return _json_response(405, {"error": "method not allowed"})
    cfg = Path(cfg_dir)
    try:
        if pathname == "/":
            return _json_response(200, _html_shell(), "text/html; charset=utf-8")
        if pathname in ("/healthz", "/api/v1/health"):
            return _json_response(200, {"status": "ok"})
        if pathname == "/api/v1/status":
            return _json_response(200, _status_projection(cfg))
        if pathname == "/api/v1/config":
            projection = _config_projection(cfg)
            if projection is None:
                return _json_response(404, {"error": "not found"})
            return _json_response(200, projection)
        if pathname == "/api/v1/runs":
            return _json_response(200, _runs_projection(cfg))
        if pathname == "/api/v1/audit/summary":
            return _json_response(200, _audit_summary_projection(cfg))
        if pathname == "/api/v1/audit":
            return _json_response(200, _audit_projection(cfg, split.query))
        if pathname == "/api/v1/checkpoint":
            return _json_response(200, _checkpoint_projection(cfg))
        run_match = _RUN_DETAIL_RE.match(pathname)
        if run_match:
            return _json_response(200, _run_projection(cfg, run_match.group(1)))
        return _json_response(404, {"error": "not found"})
    except _RouteError as exc:
        return _json_response(exc.status, {"error": str(exc)})
    except Exception:
        # Generic envelope: never leak paths, exception text, or data.
        return _json_response(500, {"error": "internal error"})


def _json_response(status: int, payload: object, content_type: str = "application/json; charset=utf-8") -> Response:
    body = payload if isinstance(payload, bytes) else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return Response(status=status, body=body, content_type=content_type)


# ---------------------------------------------------------------------------
# HTTP server (strictly 127.0.0.1)
# ---------------------------------------------------------------------------


def _is_loopback_host(host: str) -> bool:
    """True iff a ``Host`` header value names a loopback host.

    Handles optional ports (``localhost:8765``), bracketed IPv6 with a port
    (``[::1]:8765``), and IPv6 without brackets/port (``::1``). Only the
    hostname part is compared; the value is never resolved via DNS, so a
    rebinding-style foreign name can never satisfy this check. A bare
    loopback IP without a port (e.g. ``127.0.0.1``) is valid because HTTP/1.0
    clients may omit the port, and requests over the loopback socket may
    legitimately carry an absolute-form target with no Host port.
    """
    if not host:
        return False
    hostname = host.strip()
    if hostname.startswith("["):  # bracketed IPv6: [::1], [::1]:8765, [::1]:8765/
        end = hostname.find("]")
        if end == -1:
            return False
        return hostname[1:end] == "::1"
    # unbracketed: a host:port form has exactly ONE ':' (the port separator).
    # Multiple colons mean raw unbracketed IPv6 like "::1" — never split it.
    if hostname.count(":") == 1:
        head, sep, tail = hostname.partition(":")
        if tail.isdigit():
            hostname = head
    return hostname in ("127.0.0.1", "localhost", "::1")


class _WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, cfg_dir: Path):
        self.cfg_dir = cfg_dir
        super().__init__(("127.0.0.1", port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gmail-tidy-web"
    sys_version = ""

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        """Transport gate: verify the Host header names a loopback interface
        BEFORE any route resolution, method check, or filesystem access.

        The server only ever binds 127.0.0.1, but a DNS-rebinding-style request
        (or a reverse proxy pointed at a public name) can carry a foreign Host
        header over that socket. Such requests get a generic 403 (no-store)
        without touching the pure handle() seam or any data. Loopback hosts may
        carry a port (``localhost:8765``) or bracketed IPv6 (``[::1]:8765``);
        both are stripped before comparison.
        """
        host = self.headers.get("Host", "")
        if not _is_loopback_host(host):
            self._respond(Response(status=403, body=json.dumps(
                {"error": "forbidden"}).encode("utf-8")))
            return
        self._respond(handle(method, self.path, self.server.cfg_dir))

    def _respond(self, response: Response):
        body = response.body
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for key, value in response.extra_headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args):  # stderr only, never stdout
        sys.stderr.write("gmail-tidy-web: " + (fmt % args) + "\n")


def make_server(port: int, cfg_dir: Path | str) -> _WebServer:
    """Bind a loopback-only server. Raises OSError on bind failure.

    ``port == 0`` asks the OS for a random free port (read it back from
    ``server.server_address[1]``); this is the design's default for unattended
    use, so nothing listens on a well-known fixed port.
    """
    return _WebServer(int(port), Path(cfg_dir))


def serve(port: int = 8765, no_browser: bool = False, cfg_dir: Path | str | None = None) -> int:
    """Run the viewer until Ctrl-C. Returns an exit code (0 = clean shutdown).

    stdout carries ONLY the URL; anything else is logged to stderr. No secrets
    are ever printed. ``cfg_dir=None`` falls back to the standard config dir
    (the same resolution every other command uses).
    """
    cfg = Path(cfg_dir) if cfg_dir is not None else config_dir()
    server = make_server(port, cfg)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"
    print(url, flush=True)
    if not no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return EXIT_OK


__all__ = [
    "Response", "handle", "make_server", "serve",
    "AUDIT_DEFAULT_LIMIT", "AUDIT_MAX_LIMIT", "RUN_ID_RE",
]

# The static shell is defined last so it never dominates the module top half.
_SHELL_TEXT = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gmail-tidy — local viewer</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; margin: 0; padding: 1.5rem; line-height: 1.45; }
  header { display: flex; align-items: baseline; gap: .75rem; margin-bottom: 1rem; }
  h1 { font-size: 1.2rem; margin: 0; }
  h2 { font-size: 1rem; margin: 1.4rem 0 .5rem; }
  table { border-collapse: collapse; width: 100%; max-width: 56rem; }
  th, td { text-align: left; padding: .3rem .6rem; border-bottom: 1px solid #444; }
  th { font-weight: 600; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: .75rem; max-width: 56rem; }
  .card { border: 1px solid #444; border-radius: .4rem; padding: .6rem .8rem; }
  .card b { display: block; font-size: 1.4rem; }
  code { background: #222; padding: .1rem .35rem; border-radius: .25rem; }
  .err { color: #e06c75; }
  a { color: #61afef; }
</style>
</head>
<body>
<header>
  <h1>gmail-tidy local viewer</h1>
  <span id="version" class="dim"></span>
  <a href="javascript:void(0)" id="refresh">refresh</a>
</header>
<section id="status"></section>
<section>
  <h2>Checkpoint</h2>
  <table id="checkpoint"></table>
</section>
<section>
  <h2>Rules (criteria only)</h2>
  <table id="config"></table>
</section>
<section>
  <h2>Audit summary</h2>
  <table id="audit"></table>
</section>
<section>
  <h2>Runs</h2>
  <table id="runs"></table>
</section>
<section>
  <h2>Audit log (last 200)</h2>
  <table id="auditlog"></table>
</section>
<script>
const $ = (id) => document.getElementById(id);
async function jget(url) {
  const r = await fetch(url, {cache: "no-store"});
  if (!r.ok) throw new Error(url + " -> " + r.status);
  return r.json();
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g,
    (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}
function cell(v) { return "<td>" + esc(JSON.stringify(v)) + "</td>"; }
function rows(list) { return list.map((r) => "<tr>" + r + "</tr>").join(""); }
async function refresh() {
  $("refresh").textContent = "…";
  try {
    const st = await jget("/api/v1/status");
    $("version").textContent = st.config_dir;
    $("status").innerHTML = `<div class="cards">
      <div class="card"><b>${st.runs_count}</b>runs</div>
      <div class="card"><b>${esc(st.config_present ? (st.config_valid ? "valid" : "invalid") : "missing")}</b>config</div>
      <div class="card"><b>${st.token_present ? "present" : "absent"}</b>token</div>
      <div class="card"><b>${esc((st.scopes || []).length)}</b>scopes</div>
      <div class="card"><b>${st.checkpoint_present ? "yes" : "no"}</b>checkpoint</div>
      <div class="card"><b>${esc(st.latest_run || "—")}</b>latest run</div>
    </div>`;
    const ck = await jget("/api/v1/checkpoint");
    $("checkpoint").innerHTML = `<tr><th>rule</th><th>state</th></tr>` +
      Object.entries(ck.rules).map(([r, s]) => `<tr><td>${esc(r)}</td><td>${esc(s)}</td></tr>`).join("") +
      `<tr><td colspan="2">fingerprint: <code>${esc(ck.fingerprint || "—")}</code></td></tr>`;
    const cf = await jget("/api/v1/config");
    $("config").innerHTML = `<tr><th>rule</th><th>criteria</th></tr>` +
      (cf.rules || []).map((r) => `<tr><td>${esc(r.id)}</td><td>${esc(JSON.stringify(r.criteria))}</td></tr>`).join("");
    const au = await jget("/api/v1/audit/summary");
    $("audit").innerHTML = `<tr><th>by_rule</th><th>by_action</th><th>by_kind</th></tr>
      <tr>${cell(au.by_rule)}${cell(au.by_action)}${cell(au.by_kind)}</tr>`;
    const runs = await jget("/api/v1/runs");
    $("runs").innerHTML = `<tr><th>run</th></tr>` +
      (runs.runs || []).map((r) => `<tr><td>${esc(r)}</td></tr>`).join("");
    const entries = await jget("/api/v1/audit?limit=200");
    $("audit").innerHTML = `<tr><th>action</th><th>rule</th><th>kind</th><th>ts</th></tr>` +
      (entries.entries || []).slice(0, 200).map((e) =>
        `<tr>${cell(e.action)}${cell(e.rule_id)}${cell(e.kind)}${cell(Math.round(e.ts * 1000) / 1000)}</tr>`).join("");
  } catch (err) {
    $("status").innerHTML = `<p class="err">${esc(err.message)}</p>`;
  }
  $("refresh").textContent = "refresh";
}
$("refresh").addEventListener("click", refresh);
refresh();
</script>
</body>
</html>
"""
