"""Offline unit tests for the pure rendering helpers in gmail_tidy.render.

No network, no config dir, no Gmail calls: every helper transforms in-memory
Candidate / Rule objects into deterministic strings. Privacy is asserted by
feeding deliberately distinctive fake ids and proving they never reach output.
"""

import json

from gmail_tidy.audit import Candidate
from gmail_tidy.config import Actions, MatchConfig, Rule
from gmail_tidy.render import (
    action_text,
    candidate_record,
    compact_lines,
    explain_lines,
    json_text,
)


def _cand(rule_id="r1", actions=None, mid="SECRET-MSG-ID-1", tid="SECRET-THREAD-ID"):
    return Candidate(
        message_id=mid,
        thread_id=tid,
        rule_id=rule_id,
        actions=actions or Actions(add_label=["Cleanup/N"], archive=True),
        before_labels={"INBOX"},
        in_inbox=True,
    )


# --- action_text ------------------------------------------------------------


def test_action_text_add_only():
    assert action_text(Actions(add_label=["Cleanup/N"])) == "+Cleanup/N"


def test_action_text_archive_only():
    assert action_text(Actions(archive=True)) == "archive"


def test_action_text_noop():
    assert action_text(Actions()) == ""


def test_action_text_all_fields_ordered():
    text = action_text(Actions(add_label=["A", "B"], remove_label=["Promo"], archive=True))
    assert text == "+A,B, -Promo, archive"


# --- compact_lines ----------------------------------------------------------


def test_compact_groups_by_rule_and_counts():
    candidates = [
        _cand("r1", Actions(add_label=["X"], archive=True)),
        _cand("r1", Actions(add_label=["X"], archive=True)),
        _cand("r2", Actions(archive=True)),
    ]
    out = "\n".join(compact_lines("run1", candidates))
    assert "r1: 2 candidate(s)" in out
    assert "r2: 1 candidate(s)" in out
    assert "+X, archive (x2)" in out
    assert out.endswith("3 message(s). Apply with `gmail-tidy apply --yes`.")


def test_compact_first_seen_rule_order():
    candidates = [
        _cand("r2", Actions(add_label=["X"])),
        _cand("r1", Actions(add_label=["X"])),
    ]
    out = "\n".join(compact_lines("run1", candidates))
    assert out.index("r2: 1 candidate(s)") < out.index("r1: 1 candidate(s)")


def test_compact_never_leaks_ids():
    candidates = [
        _cand("SECRET-RULE", Actions(add_label=["X"]),
              mid="SECRET-MSG-ID-1", tid="SECRET-THREAD-ID"),
    ]
    out = "\n".join(compact_lines("run1", candidates))
    assert "SECRET-MSG-ID" not in out
    assert "SECRET-THREAD-ID" not in out


def test_compact_empty_candidates():
    out = "\n".join(compact_lines("run1", []))
    assert "0 message(s)." in out


# --- explain_lines ----------------------------------------------------------


def _rule(rid, **match_kwargs):
    return Rule(id=rid, match=MatchConfig(**match_kwargs), actions=Actions())


def test_explain_shows_criteria_and_not_actions():
    rules = [_rule("r1", subject_contains=["newsletter"], older_than_days=30)]
    out = "\n".join(explain_lines(rules))
    assert "Rule matching criteria" in out
    assert "r1:" in out
    assert "subject_contains: newsletter" in out
    assert "older_than_days: 30" in out


def test_explain_omits_unset_criteria():
    rules = [_rule("r1", subject_contains=["newsletter"])]
    out = "\n".join(explain_lines(rules))
    assert "from_contains" not in out
    assert "archive" not in out  # actions are never part of explain


def test_explain_unread_false_rendered():
    rules = [_rule("r1", unread=False)]
    out = "\n".join(explain_lines(rules))
    assert "unread: false" in out


def test_explain_list_criteria_joined():
    rules = [_rule("r1", labels_have=["A", "B"])]
    out = "\n".join(explain_lines(rules))
    assert "labels_have: A, B" in out


def test_explain_no_rules():
    assert explain_lines([]) == ["Rule matching criteria (from config.yaml)", "  (no rules defined)"]


def test_explain_no_criteria():
    rules = [_rule("r1")]
    out = "\n".join(explain_lines(rules))
    assert "(no criteria)" in out


# --- candidate_record / json_text ------------------------------------------


def test_json_whitelist_fields():
    rec = candidate_record(_cand(actions=Actions(add_label=["X"], archive=True)))
    assert set(rec) == {"message_id", "thread_id", "rule_id", "actions",
                        "before_labels", "in_inbox"}
    assert set(rec["actions"]) == {"add_label", "remove_label", "archive"}


def test_json_roundtrip_valid_and_deterministic():
    candidates = [
        _cand("r1", Actions(add_label=["X"], archive=True)),
        _cand("r1", Actions(add_label=["X"], archive=True)),
    ]
    text = json_text("run1", candidates)
    data = json.loads(text)
    assert data["run"] == "run1"
    assert len(data["candidates"]) == 2
    assert json_text("run1", candidates) == text  # deterministic


def test_json_excludes_unknown_fields():
    # A run file holds exactly these fields; JSON must not invent new ones.
    rec = candidate_record(_cand(actions=Actions(add_label=["X"])))
    assert "sender" not in rec
    assert "subject" not in rec
    assert "body" not in rec
    assert "size_kb" not in rec
