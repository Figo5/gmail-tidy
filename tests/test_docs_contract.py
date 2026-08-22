# tests/test_docs_contract.py
"""Contract tests pinning docs/config-reference.md to the ACTUAL code.

config-reference.md documents the ``match.query`` key and the preset
narrowing terms. These tests lock those claims to reality so the doc cannot
drift from the code without a test failure:

1. ``query`` is accepted (configs load) but ignored by ``query_from_match``
   and by rule matching;
2. a rule whose only ``match`` key is ``query`` matches EVERY fetched message
   (matches_rule returns True, so scan candidates are narrowed only by what
   the fetch query returns, never by the rule check);
3. the fetch narrowing for the four text-probe categories is the bare
   category term (``newsletters``, ``promotions``, ``receipts``,
   ``notifications``), never ``category:`` operator syntax;
4. the doc itself actually states these claims (doc <=> code lockstep).

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

# The four categories whose narrowing is probe-driven; each preset carries a
# "query" in config.PRESETS that is IGNORED, and the actual fetch term that
# query_from_match emits is the bare category name itself.
PROBE_CATEGORIES = ("newsletters", "promotions", "receipts", "notifications")
BARE_TERMS = {cat: cat for cat in PROBE_CATEGORIES}


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
    """query_from_match must not read match.query at all."""
    assert query_from_match(MatchConfig(query="category:updates")) == ""
    assert query_from_match(MatchConfig(query="anything at all")) == ""
    assert query_from_match(MatchConfig(query="x", subject_contains=["news"])) == "news"


# --- Pin 2: a query-only rule matches every fetched message ----------------


def test_query_only_rule_matches_any_meta():
    """With only query set, matches_rule is vacuous — the rule matches."""
    m = MatchConfig(query="category:updates")
    assert matches_rule(m, _meta())
    # Also true for every one of the empty/metadata-free messages.
    for cat in PROBE_CATEGORIES:
        assert matches_rule(MatchConfig(query=f"category:{cat}"), _meta())


def test_doc_states_query_only_rules_match_every_fetched_message():
    text = DOC_PATH.read_text(encoding="utf-8")
    assert "matches every fetched message" in text


# --- Pin 3: presets narrow with the bare category term, not category: ------


def test_category_narrowing_terms_are_bare():
    for cat in PROBE_CATEGORIES:
        assert query_from_match(MatchConfig(category=cat)) == cat


def test_presets_bare_terms_match_doc():
    """The doc's presets table must claim the bare term for each category."""
    text = DOC_PATH.read_text(encoding="utf-8")
    for cat in PROBE_CATEGORIES:
        assert f"bare term `{cat}`" in text
        # and must never claim the ignored category: operator syntax
        assert f"`category:{cat}`" not in text


# --- Pin 4: the doc states the narrowing/ignored claims --------------------


def test_doc_claims_query_ignored():
    text = DOC_PATH.read_text(encoding="utf-8")
    assert "currently ignored" in text
    assert "query_from_match" in text


def test_preset_metadata_matches_code():
    """config.PRESETS is the single source for what the doc's presets mean."""
    for cat in PROBE_CATEGORIES:
        assert cat in PRESETS
    assert set(PRESETS) == set(CATEGORIES)
