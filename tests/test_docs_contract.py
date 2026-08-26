# tests/test_docs_contract.py
"""Contract tests pinning docs/config-reference.md to the ACTUAL code.

config-reference.md documents the ``match.query`` key and the preset
narrowing queries. These tests lock those claims to reality so the doc cannot
drift from the code without a test failure:

1. ``query`` is accepted (configs load) but ignored by ``query_from_match``
   and by rule matching;
2. a rule whose only ``match`` key is ``query`` matches EVERY fetched message
   (matches_rule returns True, so scan candidates are narrowed only by what
   the fetch query returns, never by the rule check);
3. the fetch narrowing for the text-probe categories that HAVE a valid Gmail
   ``category:`` operator is ``PRESETS[category]['query']`` (``category:updates``,
   ``category:promotions``, ``category:purchases``), read from PRESETS as the
   single source of truth, and the doc states exactly those operator queries.
   ``notifications`` has NO valid Gmail category operator: it carries no
   ``query`` key in PRESETS, emits no narrowing term at all (the scan fetches
   everything and filters locally), and the doc must never claim a
   ``category:notifications`` operator;
4. the special categories (``old_unread``, ``large_messages``) still narrow
   with NO term at all — they have no preset query;
5. the doc itself actually states these claims (doc <=> code lockstep).

Fully offline: no sockets, no Gmail, no config-dir writes, no network.
"""

from pathlib import Path

from gmail_tidy.actions import query_from_match
from gmail_tidy.config import (
    CATEGORIES,
    PRESETS,
    MatchConfig,
    load_config,
)
from gmail_tidy.rules import MessageMeta, matches_rule

DOC_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "config-reference.md"
)

# The text-probe categories that DO carry a valid Gmail category: operator
# query; the emitted fetch term must equal PRESETS[category]['query'] — never
# the bare category name.
OPERATOR_CATEGORIES = ("newsletters", "promotions", "receipts")

# notifications is a text-probe category WITHOUT a valid Gmail category
# operator: PRESETS carries no "query" key for it, and query_from_match must
# emit NO narrowing term for a pure notifications rule.
NO_OPERATOR_CATEGORIES = ("notifications",)

# The special categories have no search term at all: query_from_match must emit
# an empty query for a pure special-category rule (fetch everything, filter
# locally), otherwise the scan would starve.
SPECIAL_CATEGORIES = ("old_unread", "large_messages")


def _meta() -> MessageMeta:
    # A maximally empty message: matches only if the rule check itself is a
    # no-op. If query were evaluated, or if query-only rules were narrowed
    # locally, this message would NOT match.
    return MessageMeta(
        id="m1",
        thread_id="t1",
        labels=set(),
        internal_date_ms=0,
        from_header="",
        to_header="",
        subject_header="",
        size_kb=0.0,
        unread=False,
    )


# --- Pin 1: query is accepted but ignored ----------------------------------


def test_query_field_exists_and_loads(tmp_path):
    """A config using match.query is valid YAML and loads without error."""
    p = tmp_path / "config.yaml"
    p.write_text(
        "rules:\n"
        "  - id: r1\n"
        "    match: {query: 'category:updates'}\n"
        "    actions: {archive: true}\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.rules[0].match.query == "category:updates"


def test_query_from_match_ignores_query_key():
    """query_from_match must not read match.query at all — even when a preset
    category would otherwise supply the fetch term."""
    assert query_from_match(MatchConfig(query="category:updates")) == ""
    assert query_from_match(MatchConfig(query="anything at all")) == ""
    assert query_from_match(MatchConfig(query="x", subject_contains=["news"])) == "news"
    # a category preset wins over a user query; the user query stays ignored
    assert query_from_match(
        MatchConfig(category="newsletters", query="user-override")
    ) == PRESETS["newsletters"]["query"]
    # and with a no-operator preset a user query still contributes nothing
    assert query_from_match(
        MatchConfig(category="notifications", query="user-override")
    ) == ""


# --- Pin 2: a query-only rule matches every fetched message -------------------


def test_query_only_rule_matches_any_meta():
    """With only query set, matches_rule is vacuous — the rule matches."""
    m = MatchConfig(query="category:updates")
    assert matches_rule(m, _meta())


def test_doc_states_query_only_rules_match_every_fetched_message():
    text = DOC_PATH.read_text(encoding="utf-8")
    assert "matches every fetched message" in text


# --- Pin 3: presets narrow with PRESETS[category]['query'] when present -------


def test_category_narrowing_terms_are_preset_queries():
    for cat in OPERATOR_CATEGORIES:
        assert PRESETS[cat]["query"]
        assert query_from_match(MatchConfig(category=cat)) == PRESETS[cat]["query"]
        # never the bare category name
        assert query_from_match(MatchConfig(category=cat)) != cat


def test_presets_queries_are_gmail_operator_forms():
    assert PRESETS["newsletters"]["query"] == "category:updates"
    assert PRESETS["promotions"]["query"] == "category:promotions"
    assert PRESETS["receipts"]["query"] == "category:purchases"


def test_notifications_has_no_operator_and_emits_nothing():
    """notifications has no valid Gmail category: operator — PRESETS carries no
    query key for it and a pure notifications rule emits an empty query."""
    assert "query" not in PRESETS["notifications"]
    assert query_from_match(MatchConfig(category="notifications")) == ""
    assert "category:notifications" not in PRESETS.get("notifications", {}).values()


def test_doc_states_each_preset_query():
    """The doc must claim the exact operator query for each operator preset."""
    text = DOC_PATH.read_text(encoding="utf-8")
    for cat in OPERATOR_CATEGORIES:
        assert PRESETS[cat]["query"] in text
    # and must never claim the ignored bare-category-term narrowing
    for cat in OPERATOR_CATEGORIES:
        assert f"bare term `{cat}`" not in text
    # and must never claim an operator for notifications (no such operator)
    assert "category:notifications" not in text


def test_doc_states_notifications_has_no_search_term():
    """The doc must state that notifications rules fetch everything and filter
    locally (no search term / no operator)."""
    text = DOC_PATH.read_text(encoding="utf-8")
    assert "notifications" in text
    # the table row for notifications must not claim a narrowing operator
    assert "narrow with `category:notifications`" not in text
    assert "no search term" in text
    assert "fetch everything" in text or "fetches everything" in text


def test_special_categories_have_no_query_and_emit_nothing():
    """The special categories must carry no 'query' in PRESETS and emit no
    fetch term — otherwise a pure special-category rule would starve."""
    for cat in SPECIAL_CATEGORIES:
        assert "query" not in PRESETS[cat]
        assert query_from_match(MatchConfig(category=cat)) == ""


# --- Pin 4: the doc states the narrowing claims ------------------------------


def test_doc_claims_query_ignored():
    text = DOC_PATH.read_text(encoding="utf-8")
    assert "ignored" in text
    assert "query_from_match" in text


def test_preset_metadata_matches_code():
    """config.PRESETS is the single source for what the doc's presets mean."""
    for cat in OPERATOR_CATEGORIES + NO_OPERATOR_CATEGORIES + SPECIAL_CATEGORIES:
        assert cat in PRESETS
    assert set(PRESETS) == set(CATEGORIES)
