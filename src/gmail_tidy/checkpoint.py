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
    """Return a fresh checkpoint if missing, corrupt, or fingerprint mismatch (config changed)."""
    fp = config_fingerprint(config)
    if not path.exists():
        return ScanCheckpoint(config_fingerprint=fp)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ScanCheckpoint(config_fingerprint=fp)
    if data.get("config_fingerprint") != fp:
        return ScanCheckpoint(config_fingerprint=fp)  # config changed; start fresh
    rules = {rid: RuleCheckpoint(page_token=r.get("page_token")) for rid, r in data.get("rules", {}).items()}
    return ScanCheckpoint(config_fingerprint=fp, rules=rules)


def save_checkpoint(path: Path, cp: ScanCheckpoint) -> None:
    payload = {
        "config_fingerprint": cp.config_fingerprint,
        "rules": {rid: {"page_token": r.page_token} for rid, r in cp.rules.items()},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass
