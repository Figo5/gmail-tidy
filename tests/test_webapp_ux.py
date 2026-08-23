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


def test_run_detail_marks_runs_nav_active():
    # Task 17: the #/run/<id> detail view maps onto the existing "runs" nav
    # link, so the Runs link is marked aria-current even though "run" is not
    # itself a nav entry. The router compares against a navView derived from
    # the parsed route view.
    js = web_shell.SHELL_JS
    assert "navView" in js
    assert 'navView = (t.view === "run") ? "runs" : t.view' in js
    assert 'a.getAttribute("data-view") === navView' in js


# ---------------------------------------------------------------------------
# Per-page document titles (Task 16)
# ---------------------------------------------------------------------------


def test_router_sets_document_title():
    # The router keeps the browser tab title in sync with the active view.
    assert "document.title" in web_shell.SHELL_JS


def test_static_html_title_stays_app_name():
    # The static <title> in the HTML is unchanged; the JS only overrides it at
    # route time with the same app-name + em dash prefix.
    assert "<title>gmail-tidy — local viewer</title>" in web_shell.SHELL_HTML
    assert "gmail-tidy — " in web_shell.SHELL_JS


def test_titles_map_covers_all_approved_views():
    import re as _re
    m = _re.search(r"var TITLES\s*=\s*\{(.*?)\}\s*;", web_shell.SHELL_JS, _re.S)
    assert m is not None, "TITLES map missing"
    body = m.group(1)
    for view in APPROVED_VIEWS:
        entry = _re.search(r"^\s*" + view + r"\s*:\s*\"([^\"]+)\"", body, _re.M)
        assert entry is not None and entry.group(1), view


def test_title_uses_parsed_view_label_with_overview_fallback():
    js = web_shell.SHELL_JS
    # The title prefix is the app name + em dash, and the label comes from the
    # parsed/validated route view; unknown hashes fall back to overview.
    assert "gmail-tidy — " in js
    assert "TITLES[t.view]" in js
    assert "TITLES.overview" in js


def test_run_title_appends_validated_run_id():
    js = web_shell.SHELL_JS
    # Run detail appends the 12-hex run id only inside the run branch, and
    # only after the existing RE_RUN validation gate passes.
    assert 't.view === "run"' in js
    assert "RE_RUN.test(t.runId)" in js
    assert 'title += " — "' in js


# ---------------------------------------------------------------------------
# Overview "Recent runs" shows the newest five (Task 18)
# ---------------------------------------------------------------------------


def test_overview_recent_runs_uses_newest_five():
    # Task 18: the overview's Recent runs table must take the LAST five run
    # ids from the /api/v1/runs list (runs.runs is newest-first), then reverse
    # so the newest is on top. This is distinct from the Runs view, which
    # reverses the full list and must be left untouched.
    js = web_shell.SHELL_JS
    assert "runs.runs.slice(-5).reverse()" in js
    # the Runs view keeps its full-list reversal
    assert "data.runs.slice().reverse()" in js


def test_no_slice_zero_five_truncation_remains():
    # The old overview bug truncated to the FIRST five (oldest of the
    # newest-first list). No slice(0, 5) truncation may remain anywhere.
    assert "slice(0, 5)" not in web_shell.SHELL_JS


# ---------------------------------------------------------------------------
# Run detail missing scan-stats state (Task 19)
# ---------------------------------------------------------------------------


def test_run_detail_missing_stats_shows_explicit_message():
    # Task 19: when the run detail response has no stats object (null or
    # undefined), the client must render an explicit "not recorded" muted
    # message via the existing el("p", "muted", ...) helper instead of
    # silently rendering nothing.
    js = web_shell.SHELL_JS
    assert "Scan stats not recorded for this run." in js
    # the guard must distinguish null/undefined stats from a present (even if
    # empty) stats object, so the old `data.stats || {}` mask that collapsed
    # missing stats into an empty object must not be used.
    assert "var stats = data.stats || {}" not in js
    assert "data.stats === null" in js
    assert "data.stats === undefined" in js


def test_run_detail_preserves_stats_table_when_present():
    # Task 19: when stats ARE present, the existing per-metric table and the
    # candidate table rendering must be preserved unchanged.
    js = web_shell.SHELL_JS
    assert 'mkTable("Scan statistics"' in js
    assert '["evaluated", "excluded", "noop", "candidates"]' in js
    assert "data.candidates" in js


# ---------------------------------------------------------------------------
# Checkpoint nested status fetch has its own catch (Task 20)
# ---------------------------------------------------------------------------


def test_checkpoint_nested_status_fetch_has_own_catch():
    # Task 20: PAGES.checkpoint fires a second, advisory jget(API.status)
    # fetch to warn when config.yaml is missing or invalid. That nested chain
    # must terminate with its OWN .catch: a status-fetch rejection is
    # advisory-only and must NOT propagate to the outer error handler, which
    # blanks the already-rendered checkpoint view (container.textContent = "").
    import re as _re
    js = web_shell.SHELL_JS
    body = _re.search(
        r"PAGES\.checkpoint\s*=\s*function\s*\(container\)\s*\{(.*?)\n\};",
        js, _re.S).group(1)
    # Cut the body at the outer (view-blanking) catch handler so the regex
    # below can only match a .catch attached to the nested status fetch
    # itself, never the outer one.
    prefix = body.split(".catch(function (err", 1)[0]
    m = _re.search(
        r"jget\(API\.status\)\.then\(function\s*\(st\)\s*\{.*?\}\)"
        r"\s*\.catch\s*\(function\s*\(\)\s*\{(.*?)\}\s*\)\s*;",
        prefix, _re.S)
    assert m is not None, (
        "the nested advisory status fetch must carry its own .catch() so a "
        "rejection does not blank the checkpoint view")
    catch_body = m.group(1)
    # The nested catch must not itself blank the view (that is the outer
    # handler's job), and the outer error handler must still be in place.
    assert 'container.textContent = ""' not in catch_body
    assert 'container.textContent = ""' in body
    assert ".catch(function (err" in body


# ---------------------------------------------------------------------------
# Shared jget helper: named timeout + AbortController (Task 21)
# ---------------------------------------------------------------------------


def test_jget_has_named_timeout_constant():
    # Task 21: the shared fetch helper must declare a single named constant so
    # the timeout is reviewable and tunable in one place; every timeout
    # reference must use the named constant, never a stray literal.
    js = web_shell.SHELL_JS
    assert "var FETCH_TIMEOUT_MS = 10000" in js
    assert js.count("FETCH_TIMEOUT_MS") >= 3
    # any other bare 10000 in the JS would be a stray timeout literal
    assert js.count("10000") == 1


def test_jget_wires_abortcontroller_signal_into_fetch():
    js = web_shell.SHELL_JS
    assert "new AbortController()" in js
    assert "controller.signal" in js
    # the fetch options must carry the abort signal alongside cache no-store
    assert "cache: \"no-store\", signal: controller.signal" in js


def test_jget_timer_aborts_after_timeout():
    # The timer is armed with the named constant and its callback must call
    # controller.abort(), which rejects the in-flight fetch.
    import re as _re
    js = web_shell.SHELL_JS
    assert "setTimeout" in js
    assert "controller.abort()" in js
    m = _re.search(
        r"setTimeout\s*\(\s*function\s*\(\)\s*\{\s*controller\.abort\(\);\s*\}\s*,\s*FETCH_TIMEOUT_MS\s*\)",
        js, _re.S)
    assert m is not None, "setTimeout(abort, FETCH_TIMEOUT_MS) missing"


def test_jget_clears_timeout_on_both_settle_paths():
    # A settled request (success, 404, or rejection) must never leave a live
    # timer: clearTimeout must run on BOTH the fulfillment and rejection
    # handlers of the fetch promise.
    js = web_shell.SHELL_JS
    assert js.count("clearTimeout") >= 2


def test_jget_preserves_404_null_contract():
    # Task 21 regression guard: a 404 must still resolve to null, never throw.
    js = web_shell.SHELL_JS
    assert 'if (r.status === 404) { return null; }' in js


def test_jget_preserves_generic_non_ok_error():
    # Task 21 regression guard: any other non-OK status still rejects with the
    # existing generic message (no per-status message table added).
    js = web_shell.SHELL_JS
    assert 'if (!r.ok) { throw new Error("Request failed (" + r.status + ")"); }' in js


def test_jget_timeout_rejects_with_explicit_message():
    # On timeout the abort rejects the fetch; the jget rejection handler must
    # convert that into a stable, explicit timeout message that flows through
    # the existing per-view .catch(error) handlers (which render err.message).
    js = web_shell.SHELL_JS
    assert '"Request timed out after " + FETCH_TIMEOUT_MS' in js
    assert "throw new Error" in js


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


def test_nav_link_touch_target():
    # Task 15: nav links are display:block so they fill the li and are the
    # entire touch/click target (not just the text run).
    import re as _re
    m = _re.search(r"nav a\s*\{([^}]*)\}", web_shell.SHELL_CSS)
    assert m is not None, "nav a rule missing"
    assert "display: block" in m.group(1)


def test_nav_link_and_button_min_height_2_75rem():
    import re as _re
    # nav link rule: 2.75rem min-height + padding so the tap target is at
    # least 44px (the 2.75rem of line-height plus vertical padding).
    m = _re.search(r"nav a\s*\{([^}]*)\}", web_shell.SHELL_CSS)
    assert m is not None, "nav a rule missing"
    assert "min-height: 2.75rem" in m.group(1)
    assert "padding" in m.group(1)
    # button rule: same min-height for the interactive controls.
    m = _re.search(r"button\s*\{([^}]*)\}", web_shell.SHELL_CSS)
    assert m is not None, "button rule missing"
    assert "min-height: 2.75rem" in m.group(1)
    assert "padding" in m.group(1)


def test_focus_visible_outline_present():
    css = web_shell.SHELL_CSS
    assert "a:focus-visible" in css
    assert "button:focus-visible" in css
    assert "outline: 2px solid" in css
    assert "outline-offset: 2px" in css


def test_focus_visible_outline_dark_theme():
    # The focus ring must stay visible in dark mode too, so the dark block
    # re-declares the outline on a light background.
    import re as _re
    dark = _re.search(
        r"@media \(prefers-color-scheme: dark\)\s*\{(.*)\}", web_shell.SHELL_CSS, _re.S)
    assert dark is not None, "dark media query missing"
    dark_block = dark.group(1)
    assert "a:focus-visible" in dark_block
    assert "button:focus-visible" in dark_block
    assert "outline: 2px solid" in dark_block


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
