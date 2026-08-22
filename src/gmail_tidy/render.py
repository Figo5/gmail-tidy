"""Pure rendering helpers for the preview command.

Everything here is deterministic, read-only, and free of I/O or network access:
the functions only transform Candidate / config objects already in memory. This
module must never import the Gmail client, auth, or touch the filesystem, so the
preview paths that use it can never trigger a Gmail call.

Privacy posture mirrors the rest of the codebase:
- compact output groups/counts by rule id and action; it never prints message
  or thread ids (nor any sender/subject/body content).
- JSON output serializes only the whitelist of fields that already exist in a
  run file (see RunJournal.save_candidates) — no new persisted fields.
- explain output shows only the match criteria configured for each rule.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import fields
from typing import Any

from gmail_tidy.audit import Candidate
from gmail_tidy.config import Actions, MatchConfig, Rule

# Fixed display order for match criteria, matching the MatchConfig dataclass.
_MATCH_FIELD_NAMES = tuple(f.name for f in fields(MatchConfig))


def action_text(actions: Actions) -> str:
    """One-line human summary of a candidate's actions."""
    parts: list[str] = []
    if actions.add_label:
        parts.append("+" + ",".join(actions.add_label))
    if actions.remove_label:
        parts.append("-" + ",".join(actions.remove_label))
    if actions.archive:
        parts.append("archive")
    return ", ".join(parts)


def compact_lines(run_id: str, candidates: list[Candidate]) -> list[str]:
    """Compact preview lines: group/count by rule, never any message id.

    Rules appear in first-seen order; within a rule, distinct action summaries
    are emitted in sorted order with counts. Deterministic for a given input.
    """
    lines = [f"Run {run_id} — proposed actions (compact)"]
    groups: dict[str, Counter] = {}
    order: list[str] = []
    for c in candidates:
        if c.rule_id not in groups:
            groups[c.rule_id] = Counter()
            order.append(c.rule_id)
        summary = action_text(c.actions) or "(no action)"
        groups[c.rule_id][summary] += 1
    for rid in order:
        total = sum(groups[rid].values())
        lines.append(f"  {rid}: {total} candidate(s)")
        for summary, n in sorted(groups[rid].items()):
            label = f"{summary} (x{n})" if n > 1 else summary
            lines.append(f"      {label}")
    lines.append(f"{len(candidates)} message(s). Apply with `gmail-tidy apply --yes`.")
    return lines


def _match_fields(match: MatchConfig) -> list[tuple[str, str]]:
    """Non-empty match criteria in a stable field order, rendered as strings.

    ``unread`` is rendered even when False (an explicit criterion); fields that
    are None or empty lists are omitted.
    """
    out: list[tuple[str, str]] = []
    for name in _MATCH_FIELD_NAMES:
        value = getattr(match, name)
        if value is None or value == []:
            continue
        if isinstance(value, list):
            rendered = ", ".join(str(v) for v in value)
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        out.append((name, rendered))
    return out


def explain_lines(rules: list[Rule]) -> list[str]:
    """Preview the match criteria of each configured rule (no actions, no data)."""
    lines = ["Rule matching criteria (from config.yaml)"]
    if not rules:
        lines.append("  (no rules defined)")
        return lines
    for rule in rules:
        lines.append(f"  {rule.id}:")
        criteria = _match_fields(rule.match)
        if not criteria:
            lines.append("      (no criteria)")
        for name, rendered in criteria:
            lines.append(f"      {name}: {rendered}")
    return lines


def candidate_record(candidate: Candidate) -> dict[str, Any]:
    """Whitelist projection of one candidate — exactly the persisted run-file fields.

    Keys intentionally mirror RunJournal.save_candidates so JSON never invents
    fields beyond what already lives in a run file.
    """
    return {
        "message_id": candidate.message_id,
        "thread_id": candidate.thread_id,
        "rule_id": candidate.rule_id,
        "actions": {
            "add_label": list(candidate.actions.add_label),
            "remove_label": list(candidate.actions.remove_label),
            "archive": candidate.actions.archive,
        },
        "before_labels": sorted(candidate.before_labels),
        "in_inbox": candidate.in_inbox,
    }


def json_text(run_id: str, candidates: list[Candidate]) -> str:
    """Serialize a run as JSON: the run id plus the candidate whitelist only."""
    payload = {"run": run_id, "candidates": [candidate_record(c) for c in candidates]}
    return json.dumps(payload, indent=2)
