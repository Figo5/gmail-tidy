"""Persist per-rule Gmail pagination state between scan invocations.

The scan command resumes from the last page it reached for each rule, so a
repeat `scan --limit N` makes forward progress through the mailbox instead of
re-fetching page 1 each time. The whole checkpoint is keyed by a stable
fingerprint of the current Config, so editing config.yaml (rules,
include/exclude) invalidates old pagination state and scanning restarts safely
from page 1 rather than silently skipping messages with a stale token.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from gmail_tidy.config import Config


@dataclass
class RuleCheckpoint:
    page_token: str | None = None
    exhausted: bool = False


@dataclass
class ScanCheckpoint:
    config_fingerprint: str
    rules: dict[str, RuleCheckpoint] = field(default_factory=dict)


def config_fingerprint(config: Config) -> str:
    """Stable hash of rules + include/exclude so editing config.yaml invalidates old checkpoints.

    Config is a plain dataclass (see gmail_tidy/config.py), so it is serialized
    with dataclasses.asdict — the same primitive audit.py uses for entries.
    """
    payload = asdict(config)
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def checkpoint_path(cfg_dir: Path) -> Path:
    return cfg_dir / "checkpoint.json"


def load_checkpoint(path: Path, config: Config) -> ScanCheckpoint:
    """Return a fresh checkpoint if missing, corrupt, or fingerprint mismatch (config changed).

    Valid-JSON-but-wrong-shape data degrades exactly like a corrupt file:
    a top-level value that is not an object, a ``rules`` field that is not an
    object, or a rule entry that is not an object are all shape errors, so a
    fresh checkpoint is returned rather than a raw AttributeError/TypeError
    leaking through scan/run/summary. Well-formed rule entries are preserved;
    only the non-dict entries are dropped.
    """
    fp = config_fingerprint(config)
    if not path.exists():
        return ScanCheckpoint(config_fingerprint=fp)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ScanCheckpoint(config_fingerprint=fp)
    if not isinstance(data, dict):
        return ScanCheckpoint(config_fingerprint=fp)  # wrong shape; start fresh
    if data.get("config_fingerprint") != fp:
        return ScanCheckpoint(config_fingerprint=fp)  # config changed; start fresh
    raw_rules = data.get("rules", {})
    if not isinstance(raw_rules, dict):
        return ScanCheckpoint(config_fingerprint=fp)  # wrong shape; start fresh
    rules = {}
    for rid, r in raw_rules.items():
        if not isinstance(r, dict):
            # A rule entry that is a string/list/scalar is a shape error:
            # drop it rather than crash on r.get().
            continue
        rules[rid] = RuleCheckpoint(page_token=r.get("page_token"),
                                    exhausted=bool(r.get("exhausted", False)))
    return ScanCheckpoint(config_fingerprint=fp, rules=rules)


def save_checkpoint(path: Path, cp: ScanCheckpoint) -> None:
    payload = {
        "config_fingerprint": cp.config_fingerprint,
        "rules": {rid: {"page_token": r.page_token, "exhausted": r.exhausted}
                  for rid, r in cp.rules.items()},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def merge_checkpoint(prior: ScanCheckpoint | None, fresh: ScanCheckpoint) -> ScanCheckpoint:
    """Merge prior checkpoint entries for rules NOT in ``fresh`` back into it.

    A --rules-scoped scan loads its checkpoint with the FULL config (so the
    fingerprint matches and unselected rules' resume state is not discarded),
    then scans only the selected rules. This helper is called right before that
    scoped checkpoint is saved: for every rule the scoped pass did NOT touch,
    the prior entry is carried forward unchanged, so checkpoint.json after a
    scoped scan still holds every previously-scanned rule. Rules the scoped pass
    DID touch keep the fresh entries. ``fresh.config_fingerprint`` (the full
    config's hash, preserved by scan) is kept. ``prior`` None is a no-op.
    """
    if prior is None:
        return fresh
    rules = dict(fresh.rules)
    for rid, entry in prior.rules.items():
        if rid not in rules:
            rules[rid] = entry
    return ScanCheckpoint(config_fingerprint=fresh.config_fingerprint, rules=rules)
