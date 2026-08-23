# tests/test_webapp_readonly.py
"""Read-only filesystem invariant tests for the pure web.handle() seam (Task 24).

The v2 web design (docs/superpowers/specs/2026-08-22-gmail-tidy-web-app-design.md,
§5 "Read-only") declares: no handler opens a file for writing; no handler calls
a gmail_tidy function with write side effects. This file proves that invariant
observationally through the pure request seam ``web.handle(method, path,
cfg_dir)``: it snapshots every path under a config fixture directory (relative
path, mtime_ns, bytes) before and after each request and asserts the tree is
byte-for-byte / content- and mtime-identical afterwards.

Coverage:

1. every GET route — including valid, unknown and invalid run ids, valid and
   invalid ``?limit=`` values, and unknown routes — leaves a populated tree
   unchanged (same bytes, same content, same mtimes, no new files, no new
   directories);
2. every route leaves an EMPTY directory empty (no files, no runs/ skeleton,
   nothing is ever created);
3. POST/PUT/DELETE/HEAD (the only methods the transport accepts besides GET)
   on /api/v1/status and on a valid run detail leave the populated tree
   unchanged (they short-circuit to 405 without touching the filesystem).

Fully offline and pure handle() only: no sockets, no network, no Gmail, no
OAuth, no live server, no browser, no apply/undo, no global config. The local
fixture writes config.yaml, a valid run + stats via RunJournal, audit.jsonl,
checkpoint.json, token.json and client_secret.json in exactly the shapes
tests/test_webapp.py uses, so the tree under test is the data the viewer
projects.
"""

import json
from pathlib import Path

import pytest

from gmail_tidy import web
from gmail_tidy.audit import Candidate, RunJournal
from gmail_tidy.config import Actions

CONFIG_TEXT = (
    "rules:\n"
    "  - id: r1\n"
    "    match: {subject_contains: [newsletter], older_than_days: 30}\n"
    "    actions:\n"
    "      add_label: [Cleanup/N]\n"
    "      archive: true\n"
)

CHECKPOINT_DOC = {
    "config_fingerprint": "fp1234",
    "rules": {
        "r1": {"page_token": None, "exhausted": True},
        "r2": {"page_token": "super-secret-page-token", "exhausted": False},
    },
}

# (route, expected GET status over a POPULATED tree). Asserting the expected
# status proves each request really exercised the intended route (a route
# crashing into 500 or 404 would pass a pure "tree unchanged" check while
# silently not testing what it claims).
GET_CASES = [
    ("/", 200),
    ("/healthz", 200),
    ("/api/v1/health", 200),
    ("/api/v1/status", 200),
    ("/api/v1/config", 200),
    ("/api/v1/runs", 200),
    ("/api/v1/runs/{run_id}", 200),          # valid run
    ("/api/v1/runs/deadbeef1234", 404),      # syntactically valid, absent run
    ("/api/v1/runs/DEADBEEF1234", 404),      # invalid run id (regex reject)
    ("/api/v1/audit", 200),
    ("/api/v1/audit?limit=1", 200),          # valid limit
    ("/api/v1/audit?limit=999999", 200),     # valid limit, clamped
    ("/api/v1/audit?limit=0", 400),          # invalid limit
    ("/api/v1/audit?limit=-3", 400),         # invalid limit
    ("/api/v1/audit?limit=abc", 400),        # invalid limit
    ("/api/v1/audit/summary", 200),
    ("/api/v1/checkpoint", 200),
    ("/api/v1/other", 404),                  # unknown route
]

NON_GET_METHODS = ("POST", "PUT", "DELETE", "HEAD")


def _populate(tmp_path: Path) -> dict:
    """Build a realistic config dir using the same fixture shapes as
    tests/test_webapp.py: config.yaml, a valid run + its stats via RunJournal,
    audit.jsonl, checkpoint.json, token.json, client_secret.json. Returns the
    created run id."""
    (tmp_path / "config.yaml").write_text(CONFIG_TEXT, encoding="utf-8")
    journal = RunJournal(tmp_path / "runs")
    run_id = journal.init_run()
    journal.save_candidates(run_id, [
        Candidate(
            message_id="m1", thread_id="t1", rule_id="r1",
            actions=Actions(add_label=["Cleanup/N"], archive=True),
            before_labels={"INBOX"}, in_inbox=True,
        ),
    ])
    journal.save_stats(run_id, {"evaluated": 3, "excluded": 1, "noop": 1, "candidates": 1})
    entry = {
        "run_id": run_id, "message_id": "m1", "thread_id": "t1", "rule_id": "r1",
        "action": "add_label", "payload": "Cleanup/N", "kind": "apply", "ts": 1720000000.0,
    }
    (tmp_path / "audit.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
    (tmp_path / "checkpoint.json").write_text(json.dumps(CHECKPOINT_DOC), encoding="utf-8")
    (tmp_path / "token.json").write_text(
        json.dumps({"token": "SECRET-ACCESS-TOKEN",
                    "scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}),
        encoding="utf-8",
    )
    (tmp_path / "client_secret.json").write_text(
        json.dumps({"installed": {"client_secret": "SUPER-SECRET"}}), encoding="utf-8")
    return {"run_id": run_id}


def _snapshot(root: Path) -> dict[str, tuple[int, bytes] | None]:
    """Record every path under root: relative path -> (mtime_ns, bytes) for
    files, None for directories (so an accidentally created EMPTY directory,
    which rglob yields as a dir, is caught too). Sorted for stable diffs."""
    snap: dict[str, tuple[int, bytes] | None] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_dir():
            snap[rel] = None
        else:
            snap[rel] = (p.stat().st_mtime_ns, p.read_bytes())
    return snap


def _assert_tree_unchanged(before: dict, after: dict) -> None:
    assert after == before, (
        "web.handle() mutated the disk tree (content or mtime changed):\n"
        f"  only-before: {sorted(set(before) - set(after))}\n"
        f"  only-after:  {sorted(set(after) - set(before))}\n"
        f"  changed:     {sorted(k for k in before if k in after and after[k] != before[k])}"
    )


# ---------------------------------------------------------------------------
# 1. Every GET route leaves a populated tree identical (bytes/content/mtime)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route,expected", GET_CASES)
def test_populated_tree_unchanged_by_every_get_route(tmp_path, route, expected):
    info = _populate(tmp_path)
    path = route.format(run_id=info["run_id"])
    before = _snapshot(tmp_path)
    assert before, "fixture tree must be non-empty before the request"
    resp = web.handle("GET", path, tmp_path)
    assert resp.status == expected, (route, resp.status, resp.body[:200])
    _assert_tree_unchanged(before, _snapshot(tmp_path))


def test_populated_tree_unchanged_by_repeated_route_mix(tmp_path):
    """A sequential mix of every route on ONE tree is the strongest invariant:
    repeated access must never accumulate writes either."""
    info = _populate(tmp_path)
    before = _snapshot(tmp_path)
    for route, expected in GET_CASES:
        path = route.format(run_id=info["run_id"])
        resp = web.handle("GET", path, tmp_path)
        assert resp.status == expected, (route, resp.status)
    _assert_tree_unchanged(before, _snapshot(tmp_path))


# ---------------------------------------------------------------------------
# 2. Every route leaves an EMPTY directory empty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route,_", GET_CASES)
def test_empty_dir_unchanged_by_every_get_route(tmp_path, route, _):
    path = route.format(run_id="deadbeef1234")  # syntactically valid, absent run
    before = _snapshot(tmp_path)
    assert before == {}
    resp = web.handle("GET", path, tmp_path)
    assert _snapshot(tmp_path) == before, f"GET {path} created files in an empty tree"
    # the route still answered normally — the invariant holds at every status
    assert resp.status in (200, 400, 404), (path, resp.status)


@pytest.mark.parametrize("method", NON_GET_METHODS)
def test_empty_dir_unchanged_by_non_get_methods(tmp_path, method):
    before = _snapshot(tmp_path)
    assert before == {}
    resp = web.handle(method, "/api/v1/status", tmp_path)
    assert resp.status == 405
    assert _snapshot(tmp_path) == before


# ---------------------------------------------------------------------------
# 3. POST/PUT/DELETE/HEAD on status + valid run detail leave the tree unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", NON_GET_METHODS)
@pytest.mark.parametrize("route", ["/api/v1/status", "/api/v1/runs/{run_id}"])
def test_non_get_leaves_populated_tree_unchanged(tmp_path, method, route):
    info = _populate(tmp_path)
    path = route.format(run_id=info["run_id"])
    before = _snapshot(tmp_path)
    resp = web.handle(method, path, tmp_path)
    assert resp.status == 405, (method, path)
    _assert_tree_unchanged(before, _snapshot(tmp_path))
