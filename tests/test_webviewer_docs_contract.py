# tests/test_webviewer_docs_contract.py
"""Contract tests pinning docs/web-viewer.md and the README's web sections to
the ACTUAL web-viewer code.

docs/web-viewer.md is the human-facing reference for the local-first, loopback-
only, read-only web viewer. These tests lock it to reality so the docs cannot
drift from the code without a test failure. Every claim is derived from the
actual source at import time (``web_shell.VIEWS``, the web_shell API route
constants, the routes ``gmail_tidy.web.handle`` resolves, and the signature of
the ``cli.web`` Typer command), never from a hardcoded name list that could
itself rot:

1. ``web_shell.VIEWS`` is the exact eight-view nav-order tuple and the doc lists
   exactly those views, in that order;
2. every API route constant and every route the ``web.handle`` seam actually
   resolves is documented, and the doc invents no route the code does not have
   (no phantom routes);
3. the ``cli.web`` command's declared options are exactly ``--port`` (default
   8765, 0–65535) and ``--no-browser`` — there is no ``--host`` — and the doc
   says so;
4. exit codes ``0``/``1``/``2`` are pinned to ``errors.EXIT_OK/EXIT_RUNTIME/
   EXIT_CONFIG`` and stated in the doc;
5. the doc pins the read-only/privacy posture: GET-only, no CORS headers, no
   cookies, ``Cache-Control: no-store``, ``token.json``/``client_secret``
   exclusions, no page tokens, and CLI-only mutations;
6. the README gains a concise ``## Web viewer`` section plus a Documentation
   link, and neither the doc nor the README section invents flags or secrets.

The docs contain only synthetic examples: no real addresses, tokens, or personal
data (asserted). Fully offline: no sockets, no Gmail, no OAuth, no live server,
no browser, no config-dir writes, no network.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import typer

from gmail_tidy import cli, errors, web, web_shell

WEB_VIEWER_PATH = Path(__file__).resolve().parent.parent / "docs" / "web-viewer.md"
README_PATH = Path(__file__).resolve().parent.parent / "README.md"

# The exact eight VIEWS the shell ships, in nav order ("run" is a detail view
# reached via #/run/<run_id>; it maps onto the "runs" nav link).
EXPECTED_VIEWS = (
    "overview",   # default
    "runs",
    "run",
    "audit",
    "rules",
    "checkpoint",
    "setup",
    "privacy",
)

# The client-route constants web_shell declares so a single edit cannot drift
# the client away from the server's route table.
EXPECTED_API_CONSTANTS = {
    "API_STATUS": "/api/v1/status",
    "API_CONFIG": "/api/v1/config",
    "API_RUNS": "/api/v1/runs",
    "API_RUN_PREFIX": "/api/v1/runs/",
    "API_AUDIT_SUMMARY": "/api/v1/audit/summary",
    "API_AUDIT_LIMIT": "/api/v1/audit?limit=200",
    "API_CHECKPOINT": "/api/v1/checkpoint",
}


def _doc() -> str:
    return WEB_VIEWER_PATH.read_text(encoding="utf-8")


def _readme_section(name: str) -> str:
    """Text of README's ``## <name>`` section (between it and the next ``##``)."""
    text = README_PATH.read_text(encoding="utf-8")
    match = re.search(rf"\n## {name}\n(.*?)(?=\n## )", text, flags=re.DOTALL)
    assert match, f"README must contain a '## {name}' section followed by a '## ' heading"
    return match.group(1)


# ---------------------------------------------------------------------------
# 1. web_shell.VIEWS is the exact eight, in nav order; the doc lists them all
# ---------------------------------------------------------------------------


def test_web_shell_views_are_exact_eight_in_nav_order():
    assert web_shell.VIEWS == EXPECTED_VIEWS
    assert len(web_shell.VIEWS) == 8


def test_doc_lists_views_in_nav_order():
    """The doc's Views table enumerates exactly the VIEWS tuple, in order."""
    text = _doc()
    match = re.search(r"\n## Views\n(.*?)(?=\n## )", text, flags=re.DOTALL)
    assert match, "docs/web-viewer.md must contain a '## Views' section"
    rows = [
        m.group(1)
        for m in re.finditer(r"^\| `([a-z][a-z0-9]*)`", match.group(1), flags=re.MULTILINE)
    ]
    assert rows == list(web_shell.VIEWS), (
        f"doc view rows {rows} != code VIEWS {list(web_shell.VIEWS)}"
    )


# ---------------------------------------------------------------------------
# 2. API route constants and the routes web.handle resolves are documented,
#    and no route the doc lists is a phantom
# ---------------------------------------------------------------------------


def test_api_route_constants_match_web_shell():
    for name, value in EXPECTED_API_CONSTANTS.items():
        assert getattr(web_shell, name) == value, name
    # The client's default audit page size constant pins the ?limit= claim.
    assert web.AUDIT_DEFAULT_LIMIT == 200
    assert web.AUDIT_MAX_LIMIT == 500


def _actual_routes() -> set[str]:
    """Every route string gmail_tidy.web.handle() can resolve.

    Derived by AST-scanning the module's own source: every string literal that
    starts with ``/`` (the fixed table) plus the run-detail regex compiled to a
    ``/api/v1/runs/{run_id}`` path. Never a hand-maintained list.
    """
    source = inspect.getsource(web)
    found = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value.startswith("/")
    }
    m = re.search(r'_RUN_DETAIL_RE = re\.compile\(r?"([^"]+)"', source)
    assert m, "web.py must define _RUN_DETAIL_RE"
    pattern = m.group(1)  # ^/api/v1/runs/([0-9a-f]{12})$
    assert pattern.startswith("^") and pattern.endswith("$")
    detail = pattern[1:-1].replace("([0-9a-f]{12})", "{run_id}")
    found.add(detail)
    return found


def _doc_routes() -> set[str]:
    """Every ``/...`` path the doc lists in its Routes table (query stripped)."""
    text = _doc()
    match = re.search(r"\n## API\b(.*?)(?=\n## )", text, flags=re.DOTALL)
    assert match, "docs/web-viewer.md must contain an '## API routes' section"
    rows = {
        m.group(1).split("?", 1)[0]
        for m in re.finditer(r"^\| `(/[^`]*)`", match.group(1), flags=re.MULTILINE)
    }
    return rows


def test_doc_routes_exactly_match_web_handle_routes():
    actual = _actual_routes()
    documented = _doc_routes()
    assert documented == actual, (
        f"doc routes differ from web.handle routes:\n"
        f"  documented-only: {sorted(documented - actual)}\n"
        f"  code-only:       {sorted(actual - documented)}"
    )


def test_doc_mentions_audit_default_and_run_prefix():
    """The client's consumed forms (?limit=200, /api/v1/runs/ prefix) are covered."""
    text = _doc()
    assert "?limit=200" in text
    assert "/api/v1/runs/" in text


# ---------------------------------------------------------------------------
# 3. cli.web signature: exactly --port (8765, 0-65535) and --no-browser, no --host
# ---------------------------------------------------------------------------


def _web_options() -> dict[str, typer.models.OptionInfo]:
    callback = next(
        (info.callback for info in cli.app.registered_commands
         if (info.name or info.callback.__name__) == "web"),
        None,
    )
    assert callback is not None, "cli.web command not registered"
    opts: dict[str, typer.models.OptionInfo] = {}
    for param in inspect.signature(callback).parameters.values():
        if isinstance(param.default, typer.models.OptionInfo):
            opts[param.name] = param.default
    return opts


def test_cli_web_signature_has_exactly_port_and_no_browser():
    opts = _web_options()
    decls = {d for opt in opts.values() for d in opt.param_decls}
    assert decls == {"--port", "--no-browser"}, f"unexpected flags declared: {decls}"
    # No --host by design: the server binds only 127.0.0.1.
    assert "--host" not in decls
    port = opts["port"]
    assert port.default == 8765
    assert port.min == 0
    assert port.max == 65535
    assert opts["no_browser"].default is False


def test_doc_documents_cli_flags_and_no_host():
    text = _doc()
    assert "--port" in text
    assert "--no-browser" in text
    assert "--port 0" in text
    assert "8765" in text
    # The doc must explicitly state there is no --host flag, never advertise one.
    assert "no `--host` flag" in text


# ---------------------------------------------------------------------------
# 4. Exit codes 0/1/2 pinned to errors and stated in the doc
# ---------------------------------------------------------------------------


def test_exit_codes_match_errors_constants():
    assert (errors.EXIT_OK, errors.EXIT_RUNTIME, errors.EXIT_CONFIG) == (0, 1, 2)


def test_doc_states_exit_codes():
    text = _doc()
    assert "`0` clean shutdown" in text
    assert "`1` bind or startup failure" in text
    assert "`2` usage error" in text


# ---------------------------------------------------------------------------
# 5. Read-only / privacy posture pinned in the doc
# ---------------------------------------------------------------------------

_PINNED_STATEMENTS = (
    "local-first",        # local-first loopback-only read-only viewer
    "loopback-only",
    "127.0.0.1",
    "read-only",
    "GET-only",           # no side-effectful endpoints
    "no CORS headers",    # no Access-Control-Allow-Origin
    "no cookies",
    "no-store",           # Cache-Control: no-store on every response
    "token.json",         # presence/scopes only, bytes never read
    "client_secret",      # never read, never served
    "never",              # the exclusion statements all use "never"
    "page tokens",        # checkpoint page tokens are never shown
    "CLI",                # every mutation happens through the CLI
    "example.com",        # synthetic example only, never a real address
)


def test_doc_pins_read_only_and_privacy_statements():
    text = _doc().lower()
    for statement in _PINNED_STATEMENTS:
        assert statement.lower() in text, f"docs/web-viewer.md must state: {statement!r}"
def test_doc_has_no_secrets_or_personal_data():
    text = _doc().lower()
    for forbidden in ("@gmail.com", "@example.net", "password", "ya29.", "api key"):
        assert forbidden not in text, f"docs/web-viewer.md must not contain {forbidden!r}"


# ---------------------------------------------------------------------------
# 6. README gains a short ## Web viewer section plus a Documentation link
# ---------------------------------------------------------------------------


def test_readme_has_web_viewer_section_linking_the_doc():
    section = _readme_section("Web viewer")
    assert "docs/web-viewer.md" in section
    # The section stays accurate: it must not invent flags the CLI lacks.
    assert "--host" not in section


def test_readme_documentation_links_web_viewer_doc():
    section = _readme_section("Documentation")
    assert "docs/web-viewer.md" in section
    assert "web-viewer" in section
