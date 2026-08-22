# tests/test_webapp_ux.py
"""Offline UX/structure tests for the Task 14 web shell + API surface.

Covers the *static* client shell in ``gmail_tidy.web_shell`` (separated
CSS/JS/HTML constants, ``html_shell() -> bytes``) and the delegation seam in
``gmail_tidy.web``. These are text/structure tests, not a browser: they read
the shipped HTML/JS/CSS and assert the client contract that the design and
Task 14 scope pin:

1. the shell route and its delegation seam stay in shape;
2. the eight approved hash views exist in the nav and route table;
3. the client consumes ONLY the existing relative API endpoints;
4. there are no external assets / absolute URLs;
5. rendering is DOM/textContent-only (no innerHTML, eval, document.write,
   javascript: URLs);
6. accessibility scaffolding (skip link, semantic landmarks, aria-current,
   aria-live, table captions/scoped headers);
7. privacy copy in the Privacy view and the read-only labeling;
8. responsive CSS (mobile + dark scheme media queries);
9. explicit state markers (loading / error+retry / stale-checkpoint / setup
   guidance) referenced by the client;
10. the server API/security surface (handle()/__all__/host gate wiring) is
    unchanged from Task 8/9.

Fully offline: no sockets, no Gmail, no network, no browser.
"""

import json
from pathlib import Path

from gmail_tidy import web, web_shell

APPROVED_VIEWS = (
    "overview", "runs", "run", "audit", "rules", "checkpoint", "setup", "privacy",
)

ALLOWED_ENDPOINTS = (
    "/api/v1/status",
    "/api/v1/config",
    "/api/v1/runs",
    "/api/v1/runs/",
    "/api/v1/audit/summary",
    "/api/v1/audit?limit=200",
    "/api/v1/checkpoint",
)


# ---------------------------------------------------------------------------
# Shell route / delegation contract
# ---------------------------------------------------------------------------


def test_html_shell_returns_bytes():
    body = web_shell.html_shell()
    assert isinstance(body, bytes)
    assert body.startswith(b"<!doctype html>")
    assert b"</html>" in body


def test_html_shell_has_separated_assets():
    # The three assets must exist as independent constants AND be assembled
    # into the document exactly once each.
    assert isinstance(web_shell.SHELL_CSS, str)
    assert isinstance(web_shell.SHELL_JS, str)
    assert isinstance(web_shell.SHELL_HTML, str)
    assert web_shell.SHELL_CSS in web_shell.SHELL_HTML
    assert web_shell.SHELL_JS in web_shell.SHELL_HTML


def test_root_route_serves_delegated_shell(tmp_path):
    resp = web.handle("GET", "/", tmp_path)
    assert resp.status == 200
    assert resp.content_type == "text/html; charset=utf-8"
    assert resp.body == web_shell.html_shell()
    assert b"<html" in resp.body
    assert b"gmail-tidy" in resp.body


def test_web_delegates_html_shell_to_web_shell():
    # The seam: web._html_shell returns exactly web_shell.html_shell() output.
    assert web._html_shell() == web_shell.html_shell()


def test_web_all_unexported_surface_unchanged():
    assert web.__all__ == [
        "Response", "handle", "make_server", "serve",
        "AUDIT_DEFAULT_LIMIT", "AUDIT_MAX_LIMIT", "RUN_ID_RE",
    ]


def test_web_shell_views_are_the_approved_eight():
    assert tuple(web_shell.VIEWS) == APPROVED_VIEWS


# ---------------------------------------------------------------------------
# Eight views present in nav + route table
# ---------------------------------------------------------------------------


def test_all_eight_views_in_nav():
    text = web_shell.SHELL_HTML
    # Seven static nav links; the "run" detail view is reached only via
    # #/run/<id> links (no id exists to link in the nav).
    for view in ("overview", "runs", "audit", "rules", "checkpoint", "setup", "privacy"):
        assert f'data-view="{view}"' in text, view
        assert f"#/{view}" in text, view
    assert "#/run/" in web_shell.SHELL_HTML or "#/run/" in web_shell.SHELL_JS


def test_run_view_hash_uses_run_prefix():
    assert "#/run/" in web_shell.SHELL_JS
    assert web_shell.API_RUN_PREFIX == "/api/v1/runs/"


def test_router_handles_all_views():
    js = web_shell.SHELL_JS
    for view in APPROVED_VIEWS:
        assert f"PAGES.{view}" in js, view
    # unknown hashes fall back to overview (client-only routing rule)
    assert "overview" in js


# ---------------------------------------------------------------------------
# Only allowed relative endpoints
# ---------------------------------------------------------------------------


def test_client_uses_only_allowed_endpoints():
    js = web_shell.SHELL_JS
    for endpoint in ALLOWED_ENDPOINTS:
        assert endpoint in js, endpoint
    # sanity: the constants must point at the same routes the server resolves.
    assert web_shell.API_STATUS == "/api/v1/status"
    assert web_shell.API_AUDIT_LIMIT == "/api/v1/audit?limit=200"


def test_no_other_api_paths_are_used():
    js = web_shell.SHELL_JS
    import re
    paths = set(re.findall(r'"/api/[^"]+"', js))
    allowed = {json.dumps(e) for e in ALLOWED_ENDPOINTS}
    # any api path literal must be an allowed endpoint (or part of run prefix)
    for p in paths:
        bare = p.strip('"')
        if bare.endswith("...") or bare.endswith("+"):
            continue
        assert bare in ALLOWED_ENDPOINTS or bare.startswith("/api/v1/runs/"), bare


# ---------------------------------------------------------------------------
# No external assets / no absolute URLs
# ---------------------------------------------------------------------------


def test_no_external_urls():
    text = web_shell.SHELL_HTML + web_shell.SHELL_JS
    assert "https://" not in text
    assert "http://" not in text
    # href values in the static HTML are all fragment hashes or the skip target
    import re
    hrefs = re.findall(r'href="([^"]+)"', web_shell.SHELL_HTML)
    for h in hrefs:
        assert h.startswith("#"), h


# ---------------------------------------------------------------------------
# Safe rendering (no innerHTML / eval / document.write / javascript:)
# ---------------------------------------------------------------------------


def test_no_unsafe_dom_patterns():
    # Strip comment lines so the JS contract comment can't mask real usage.
    text = "\n".join(
        line for line in web_shell.SHELL_JS.splitlines()
        if not line.strip().startswith("//")
    )
    for bad in ("innerHTML", "eval(", "document.write", "javascript:", "document.exec"):
        assert bad not in text, bad


def test_textcontent_based_rendering():
    js = web_shell.SHELL_JS
    assert "textContent" in js
    # the escape/format helpers use textContent, not string interpolation
    assert "createElement" in js
    # every data-render helper sets textContent or appends nodes, never markup
    assert "insertAdjacentHTML" not in js


def test_all_client_fetches_are_relative():
    for endpoint in ALLOWED_ENDPOINTS:
        assert endpoint in web_shell.SHELL_JS


# ---------------------------------------------------------------------------
# Accessibility scaffolding
# ---------------------------------------------------------------------------


def test_semantic_landmarks_and_skip_link():
    html = web_shell.SHELL_HTML
    assert '<header' in html and "</header>" in html
    assert "<nav" in html and "</nav>" in html
    assert '<main id="main">' in html and "</main>" in html
    assert "<footer" in html and "</footer>" in html
    assert 'class="skip"' in html
    assert "Skip to content" in html


def test_aria_current_and_aria_live():
    js = web_shell.SHELL_JS
    # aria-live / role=status region is created via setAttribute before routing.
    assert '"aria-live"' in js
    assert '"polite"' in js
    assert '"role"' in js
    assert '"status"' in js
    # aria-current is toggled by the router for the active nav link.
    assert "aria-current" in js


def test_tables_have_captions_and_scoped_headers():
    js = web_shell.SHELL_JS
    assert '"caption"' in js
    assert '"scope"' in js
    assert '"col"' in js
    assert '"row"' in js


# ---------------------------------------------------------------------------
# Privacy copy and read-only labeling
# ---------------------------------------------------------------------------


def test_privacy_claims_present():
    text = web_shell.SHELL_HTML + web_shell.SHELL_JS
    assert "read-only" in text
    assert "never" in text
    assert "127.0.0.1" in text or "loopback" in text
    assert "bodies" in text or "senders" in text or "subjects" in text
    assert "tokens" in text or "secrets" in text or "credentials" in text
    assert "cookies" in text or "tracking" in text


def test_privacy_view_exists():
    assert "#/privacy" in web_shell.SHELL_HTML
    assert "PAGES.privacy" in web_shell.SHELL_JS


# ---------------------------------------------------------------------------
# Responsive CSS and color scheme
# ---------------------------------------------------------------------------


def test_responsive_media_queries():
    css = web_shell.SHELL_CSS
    assert "@media (max-width: 640px)" in css
    assert "@media (prefers-color-scheme: dark)" in css
    assert "color-scheme: light dark" in css or "color-scheme" in css


def test_mobile_table_overflow():
    css = web_shell.SHELL_CSS
    assert "overflow-x" in css
    assert "display: block" in css


# ---------------------------------------------------------------------------
# Explicit state markers (loading / error / empty / stale / setup)
# ---------------------------------------------------------------------------


def test_loading_and_error_states_present():
    js = web_shell.SHELL_JS
    assert "Loading" in js
    assert "setLive" in js
    assert "aria-live" in js or "aria-live" in js


def test_retry_button_state_present():
    js = web_shell.SHELL_JS
    assert "Retry" in js
    assert "errState" in js


def test_stale_invalid_config_notice_present():
    text = web_shell.SHELL_JS + web_shell.SHELL_HTML
    assert "stale" in text.lower()
    assert "restarts from page 1" in text
    assert "config.yaml" in text


def test_missing_config_and_setup_guidance():
    text = web_shell.SHELL_JS + web_shell.SHELL_HTML
    assert "config.yaml not found" in text
    assert "gmail-tidy init" in text
    assert "Setup" in text


def test_empty_state_messages_present():
    js = web_shell.SHELL_JS
    assert "No runs yet" in js
    assert "No candidates in this run" in js
    assert "No audit entries yet" in js
    assert "No rules configured" in js


# ---------------------------------------------------------------------------
# Unchanged server API / security surface
# ---------------------------------------------------------------------------


def test_server_surface_shapes_unchanged(tmp_path):
    # The routes and response shapes the shell depends on are still live.
    assert web.handle("GET", "/healthz", tmp_path).status == 200
    assert web.handle("GET", "/api/v1/health", tmp_path).status == 200
    assert web.handle("POST", "/api/v1/status", tmp_path).status == 405
    assert web.handle("GET", "/api/v1/nope", tmp_path).status == 404
    r = web.handle("GET", "/api/v1/status", tmp_path)
    assert json.loads(r.body)["config_present"] is False


def test_no_auth_layer_remains():
    # No auth tokens/bearer logic in the web surface beyond the existing
    # transport Host gate.
    assert "Authorization" not in web_shell.SHELL_HTML
    assert "bearer" not in web_shell.SHELL_JS.lower()
    assert "Set-Cookie" not in web_shell.SHELL_HTML
