# tests/test_rules.py
from datetime import datetime, timezone
from gmail_tidy.config import MatchConfig, Actions, Rule, Config
from gmail_tidy.rules import MessageMeta, matches_rule, is_excluded, is_included, first_matching_rule


def _meta(**kw):
    base = dict(id="m1", thread_id="t1", labels={"INBOX"}, internal_date_ms=0,
                from_header="sender@example.com", to_header="you@example.com",
                subject_header="", size_kb=10.0, unread=False)
    base.update(kw)
    return MessageMeta(**base)


def test_category_newsletters():
    m = MatchConfig(category="newsletters")
    assert matches_rule(m, _meta(from_header="news@example.com", subject_header="Your Digest"))
    assert not matches_rule(m, _meta(from_header="boss@example.com"))


def test_older_than_days():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    m = MatchConfig(older_than_days=30)
    assert matches_rule(m, _meta(internal_date_ms=now_ms - 40 * 86_400_000))
    assert not matches_rule(m, _meta(internal_date_ms=now_ms - 1 * 86_400_000))


def test_include_gate_and_exclude_override():
    cfg = Config(include=["label:work"], exclude=[MatchConfig(from_contains=["bank"])])
    assert is_excluded(cfg, _meta(labels={"work"}, from_header="bank@example.com"))
    assert not is_included(cfg, _meta(labels={"INBOX"}))
    assert is_included(cfg, _meta(labels={"work"}))


def test_protected_labels_exclude():
    cfg = Config()
    assert is_excluded(cfg, _meta(labels={"IMPORTANT"}))


def test_first_matching_rule_wins():
    r1 = Rule(id="r1", match=MatchConfig(unread=True), actions=Actions(archive=True))
    r2 = Rule(id="r2", match=MatchConfig(category="newsletters"), actions=Actions())
    cfg = Config(rules=[r1, r2])
    assert first_matching_rule(cfg, _meta(unread=True)) is r1
    assert first_matching_rule(cfg, _meta(from_header="newsletter@example.com")) is r2
    assert first_matching_rule(cfg, _meta(unread=False, from_header="boss@example.com")) is None
