# tests/test_forbidden_api.py
"""AST-level gate: gmail-tidy's source may only call the allowed Gmail API surface.

This is a precise AST test, not a grep: plain words ("delete", "trash") in
docstrings, help text, comments, or variable names never trigger it. Only an
actual method call on a Gmail resource object (users/messages/labels/threads)
is examined.
"""

import ast
from pathlib import Path

ALLOWED_METHODS = {"list", "get", "batchModify", "create", "getProfile"}
FORBIDDEN_METHODS = {
    "delete", "trash", "untrash", "send", "import_", "batchDelete",
    "modify", "stop", "watch",
}
# resources that are entirely off-limits when reached through users()
FORBIDDEN_RESOURCES = {"drafts", "settings"}
RESOURCE_NAMES = {"users", "messages", "labels", "threads"}


def _chain(node) -> list[str]:
    """Full dotted chain: attribute accesses and callable resolutions.

    E.g. svc.users().messages().delete(...) -> [delete, messages, users, svc]
    (call arguments are skipped — only the receiver chain is walked).
    """
    parts: list[str] = []
    cur = node
    while True:
        if isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        elif isinstance(cur, ast.Call):
            cur = cur.func  # unwrap intermediate calls (users(), messages(), ...)
        elif isinstance(cur, ast.Name):
            parts.append(cur.id)
            break
        else:
            break
    return parts


def _find_forbidden(source: str) -> list[list[str]]:
    tree = ast.parse(source)
    hits: list[list[str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        chain = _chain(node.func)
        if not any(r in chain for r in RESOURCE_NAMES):
            continue
        # any forbidden sub-resource anywhere in the chain (drafts.*, settings.*)
        if any(r in FORBIDDEN_RESOURCES for r in chain):
            hits.append(chain)
            continue
        terminal = chain[0]  # the final method name (or a bare resource access)
        if terminal in FORBIDDEN_METHODS or terminal not in (ALLOWED_METHODS | RESOURCE_NAMES):
            hits.append(chain)
    return hits


def test_helper_flags_real_resource_calls():
    assert _find_forbidden('svc.users().messages().delete(id="x")') != []
    assert _find_forbidden('svc.users().messages().send(body={})') != []
    assert _find_forbidden('svc.users().settings().updateAutoForwarding({})') != []
    assert _find_forbidden('svc.users().drafts().send({})') != []
    assert _find_forbidden('svc.threads().delete(id="x")') != []


def test_helper_ignores_words_and_variables():
    assert _find_forbidden('print("delete", "trash", "spam")') == []
    assert _find_forbidden('def trash(): pass\nx = "send"') == []
    assert _find_forbidden('label = "Cleanup/delete"') == []


def test_helper_allows_surface():
    assert _find_forbidden('svc.users().messages().batchModify(body={})') == []
    assert _find_forbidden('svc.users().messages().list(q="x")') == []
    assert _find_forbidden('svc.users().labels().create(body={})') == []
    assert _find_forbidden('svc.users().getProfile()') == []


def test_real_source_has_no_forbidden_calls():
    root = Path(__file__).parent.parent / "src"
    hits: list[tuple[str, list[str]]] = []
    for py in root.rglob("*.py"):
        for chain in _find_forbidden(py.read_text(encoding="utf-8")):
            hits.append((str(py.relative_to(root)), chain))
    assert hits == [], f"forbidden Gmail API calls found: {hits}"
