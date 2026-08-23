"""Audit log (durable, minimal) and per-run journal (checkpoints, resume)."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from gmail_tidy.config import Actions


def _chmod_600(path: Path) -> None:
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass


@dataclass
class AuditEntry:
    run_id: str
    message_id: str
    thread_id: str
    rule_id: str
    action: str
    payload: str | None = None
    kind: str = "apply"
    ts: float = field(default_factory=lambda: time.time())


class AuditLog:
    """Append-only JSONL. Never stores sender/subject/body/size/content."""

    def __init__(self, path: Path):
        self.path = path
        if os.name != "nt":
            try:
                self.path.touch(exist_ok=True)
                _chmod_600(self.path)
            except OSError:
                pass

    def append(self, entry: AuditEntry) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(entry)) + "\n")

    def entries(self) -> list[AuditEntry]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as fh:
            return [AuditEntry(**json.loads(line)) for line in fh if line.strip()]


@dataclass
class Candidate:
    message_id: str
    thread_id: str
    rule_id: str
    actions: Actions
    before_labels: set[str] = field(default_factory=set)
    in_inbox: bool = True


class RunJournal:
    def __init__(self, dir: Path):
        self.dir = dir

    def init_run(self) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex[:12]
        (self.dir / f"{run_id}.json").write_text("[]", encoding="utf-8")
        return run_id

    def save_candidates(self, run_id: str, candidates: list[Candidate]) -> None:
        data = [
            {
                "message_id": c.message_id,
                "thread_id": c.thread_id,
                "rule_id": c.rule_id,
                "actions": {
                    "add_label": c.actions.add_label,
                    "remove_label": c.actions.remove_label,
                    "archive": c.actions.archive,
                },
                "before_labels": sorted(c.before_labels),
                "in_inbox": c.in_inbox,
            }
            for c in candidates
        ]
        path = self.dir / f"{run_id}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        _chmod_600(path)

    def load_candidates(self, run_id: str) -> list[Candidate]:
        path = self.dir / f"{run_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"run {run_id} not found")
        data = json.loads(path.read_text(encoding="utf-8"))
        return [
            Candidate(
                message_id=d["message_id"],
                thread_id=d["thread_id"],
                rule_id=d["rule_id"],
                actions=Actions(**d["actions"]),
                before_labels=set(d["before_labels"]),
                in_inbox=d["in_inbox"],
            )
            for d in data
        ]

    def save_stats(self, run_id: str, stats: dict) -> None:
        """Persist aggregate ScanStats counts (evaluated/excluded/noop/candidates).

        Accepts a plain dict (asdict(ScanStats)) to avoid importing actions.ScanStats
        here — actions imports audit, so the reverse import would be circular.
        """
        path = self.dir / f"{run_id}.stats.json"
        path.write_text(json.dumps(stats), encoding="utf-8")
        _chmod_600(path)

    def load_stats(self, run_id: str) -> dict | None:
        """Return the persisted stats dict for a run, or None if never saved (old runs)."""
        path = self.dir / f"{run_id}.stats.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def record_failure(self, run_id: str, message_id: str, err: str) -> None:
        path = self.dir / f"{run_id}.failures.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"message_id": message_id, "err": err}) + "\n")

    def failures(self, run_id: str) -> list[str]:
        """Read a run's recorded per-message failures as ``message_id: err`` lines.

        Defensively skips blank lines and lines that are not valid
        ``{"message_id": str, "err": str}`` records (malformed JSON, missing or
        non-string keys, non-objects) — a corrupted ``.failures.jsonl`` must
        never crash summary/apply/run and never surface partial data. The
        remaining valid records are returned in file order.
        """
        path = self.dir / f"{run_id}.failures.jsonl"
        if not path.exists():
            return []
        out: list[str] = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):  # JSONDecodeError and malformed input
                    continue
                if not isinstance(rec, dict):
                    continue
                mid = rec.get("message_id")
                err = rec.get("err")
                if not isinstance(mid, str) or not isinstance(err, str):
                    continue
                out.append(f"{mid}: {err}")
        return out

    def list_runs(self) -> list[str]:
        if not self.dir.exists():
            return []
        files = [p for p in self.dir.glob("*.json")
                 if not p.name.endswith(".stats.json")]  # exclude companion stats files
        files.sort(key=lambda p: p.stat().st_mtime)  # chronological, oldest first
        return [p.stem for p in files]
