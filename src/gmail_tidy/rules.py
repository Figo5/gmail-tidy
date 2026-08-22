"""Pure rule evaluation over message metadata. No network, no Gmail calls.

Only metadata (labels, headers, size, internalDate, unread) is evaluated —
search queries narrow the candidate set only and are never the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from gmail_tidy.config import CATEGORIES, Config, MatchConfig, Rule

PROTECTED_AT_RUNTIME = frozenset(
    {"IMPORTANT", "STARRED", "SPAM", "TRASH", "DRAFT", "SENT", "CHAT"}
)

# Text-probe categories: matched by scanning From/Subject for these substrings.
# old_unread and large_messages are special cases handled in _category_hits, so
# they intentionally have no entry here. CATEGORIES is the shared source of truth
# for the complete set (see gmail_tidy.config); _TEXT_PROBES + special cases must
# always cover CATEGORIES exactly.
_TEXT_PROBES: dict[str, list[str]] = {
    "newsletters": ["newsletter", "digest", "unsubscribe"],
    "promotions": ["promotion", "sale", "offer", "discount"],
    "receipts": ["receipt", "order", "invoice", "payment"],
    "notifications": ["notification", "alert"],
}

# Every canonical category must be either probe-driven or a special case below;
# if a preset is added to config.PRESETS this fails loudly instead of silently
# matching nothing.
_SPECIAL_CATEGORIES = frozenset({"old_unread", "large_messages"})
if set(_TEXT_PROBES) | _SPECIAL_CATEGORIES != set(CATEGORIES):
    raise RuntimeError(
        "rules categories no longer match config.CATEGORIES: "
        f"probes={sorted(_TEXT_PROBES)} special={sorted(_SPECIAL_CATEGORIES)} "
        f"categories={sorted(CATEGORIES)}"
    )


@dataclass
class MessageMeta:
    id: str
    thread_id: str
    labels: set[str]
    internal_date_ms: int
    from_header: str | None
    to_header: str | None
    subject_header: str | None
    size_kb: float
    unread: bool


def _days_ago_ms(days: int) -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000) - days * 86_400_000


def _category_hits(category: str, meta: MessageMeta) -> bool:
    if category == "old_unread":
        return meta.unread
    if category == "large_messages":
        return meta.size_kb >= 1024
    text = f"{meta.from_header or ''} {meta.subject_header or ''}".lower()
    probes = _TEXT_PROBES.get(category, [])
    return any(p in text for p in probes)


def matches_rule(match: MatchConfig, meta: MessageMeta) -> bool:
    if match.category and not _category_hits(match.category, meta):
        return False
    if match.from_contains and not any(s.lower() in (meta.from_header or "").lower()
                                        for s in match.from_contains):
        return False
    if match.from_ends and not any((meta.from_header or "").lower().endswith(s.lower())
                                    for s in match.from_ends):
        return False
    if match.to_contains and not any(s.lower() in (meta.to_header or "").lower()
                                      for s in match.to_contains):
        return False
    if match.subject_contains and not any(s.lower() in (meta.subject_header or "").lower()
                                           for s in match.subject_contains):
        return False
    if match.labels_have and not set(match.labels_have) <= meta.labels:
        return False
    if match.labels_missing and set(match.labels_missing) & meta.labels:
        return False
    if match.older_than_days and meta.internal_date_ms >= _days_ago_ms(match.older_than_days):
        return False
    if match.newer_than_days and meta.internal_date_ms < _days_ago_ms(match.newer_than_days):
        return False
    if match.larger_than_kb and meta.size_kb < match.larger_than_kb:
        return False
    if match.unread is not None and meta.unread != match.unread:
        return False
    return True


def matches_any(matches: list[MatchConfig], meta: MessageMeta) -> bool:
    return any(matches_rule(m, meta) for m in matches)


def is_excluded(config: Config, meta: MessageMeta) -> bool:
    if matches_any(config.exclude, meta):
        return True
    return bool(meta.labels & PROTECTED_AT_RUNTIME)


def is_included(config: Config, meta: MessageMeta) -> bool:
    if not config.include:
        return True
    if any(q.startswith("label:") for q in config.include):
        names = {q.split(":", 1)[1] for q in config.include if q.startswith("label:")}
        return bool(names & meta.labels)
    text = f"{meta.from_header or ''} {meta.subject_header or ''}".lower()
    return any(q.lower() in text for q in config.include)


def first_matching_rule(config: Config, meta: MessageMeta) -> Rule | None:
    if is_excluded(config, meta) or not is_included(config, meta):
        return None
    for rule in config.rules:
        if matches_rule(rule.match, meta):
            return rule
    return None
