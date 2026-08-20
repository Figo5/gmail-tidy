# gmail-tidy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `gmail-tidy`, a privacy-conscious Python CLI that applies declarative rules to existing Gmail mail, with label add/remove and archive as the only write actions.

**Architecture:** Plan vs. play. `scan` builds a candidate plan locally (pure rule evaluation over fetched metadata) and persists it in a run journal; `apply` is the only command that mutates Gmail (via `users.messages.batchModify`, ≤1000 ids/batch, retried, checkpointed); `undo` reverses a run from its before-state snapshot, skipping messages the user changed since. A `MockGmailApi` in `tests/mock_gmail.py` provides a fully offline test surface; a dedicated AST test proves no destructive Gmail method is ever callable.

**Tech Stack:** Python ≥3.11 (machine has 3.14.7), Typer + Rich, PyYAML + Pydantic v2, google-auth-oauthlib + google-api-python-client, pytest. Editable install via hatchling.

## Global Constraints

- Python `>=3.11`; package name `gmail-tidy`; src layout `src/gmail_tidy/`.
- The **only** Gmail write is `users.messages.batchModify` (label add/remove, ≤1000 ids/batch). **No delete/trash/spam/send/import anywhere in `src/`** — enforced by `tests/test_forbidden_api.py` (AST, not grep).
- Allowed API surface: `messages.list`, `messages.get` (format=metadata), `messages.batchModify`, `labels.list`, `labels.get`, `labels.create`, `users.getProfile`.
- Never-touch labels: `IMPORTANT`, `STARRED`, `SPAM`, `TRASH`, `DRAFT`, `SENT`, `CHAT`, plus tool-created `Cleanup/*`. Naming one in `remove_label` is a config-load error (exit 2).
- Audit log JSONL fields only: `ts, run_id, message_id, thread_id, rule_id, action, payload, kind`. **Never** sender/subject/body/size/content.
- Run files, audit log, `token.json`, `client_secret*.json` are `chmod 600` in the config dir; never committed.
- `apply`/`undo` require confirmation (`--yes` skips); `preview`/`undo` default to dry-run (no writes).
- Normal commands talk to Gmail by design ("dry-run" = no writes, not no network). `--live` gates `tests/live/` integration only; CI never runs it.
- Exit codes: `0` ok · `1` runtime · `2` config/usage · `3` nothing to do · `4` auth · `5` cancelled · `6` partial.
- OAuth: `gmail.readonly` for scan/preview/status; `gmail.modify` + `gmail.labels` for apply/undo. Escalation = fresh interactive consent; `auth revoke` removes the local token safely.
- Presets ship disabled-by-default. Fixtures/docs use only synthetic addresses (`example.com`).

---

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `LICENSE` (MIT), `src/gmail_tidy/__init__.py`, `src/gmail_tidy/errors.py`, `tests/test_smoke.py`

**Interfaces:**
- Produces: installable `gmail-tidy` package; `from gmail_tidy.errors import EXIT_OK, ..., TidyError, ConfigError, AuthError, NoWorkError, RequestError, PartialError`.

- [ ] **Step 1: Write the failing smoke test**

```python
# tests/test_smoke.py
from gmail_tidy.errors import (
    EXIT_OK, EXIT_RUNTIME, EXIT_CONFIG, EXIT_NOOP,
    EXIT_AUTH, EXIT_CANCELLED, EXIT_PARTIAL,
    ConfigError, AuthError, NoWorkError, RequestError, PartialError, TidyError,
)

def test_exit_codes():
    assert (EXIT_OK, EXIT_RUNTIME, EXIT_CONFIG) == (0, 1, 2)
    assert (EXIT_NOOP, EXIT_AUTH, EXIT_CANCELLED, EXIT_PARTIAL) == (3, 4, 5, 6)

def test_error_hierarchy():
    for cls in (ConfigError, AuthError, NoWorkError, RequestError, PartialError):
        assert issubclass(cls, TidyError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_smoke.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gmail_tidy'`

- [ ] **Step 3: Create project files**

`pyproject.toml`:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "gmail-tidy"
version = "0.1.0"
description = "Privacy-conscious declarative cleanup for existing Gmail mail — label + archive only."
requires-python = ">=3.11"
dependencies = [
  "typer>=0.12",
  "rich>=13.7",
  "pyyaml>=6.0",
  "pydantic>=2.6",
  "google-auth-oauthlib>=1.2",
  "google-api-python-client>=2.120",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-cov>=5"]

[tool.hatch.build.targets.wheel]
packages = ["src/gmail_tidy"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

`.gitignore`:

```gitignore
__pycache__/
*.py[cod]
.venv/
dist/
build/
*.egg-info/
client_secret*.json
token.json
*.local
tests/.live/
```

`LICENSE`: MIT text, copyright `2026 gmail-tidy contributors`.

`src/gmail_tidy/__init__.py`:

```python
__version__ = "0.1.0"
```

`src/gmail_tidy/errors.py`:

```python
"""Exit codes and error hierarchy. Exit codes are stable, public API."""

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2
EXIT_NOOP = 3
EXIT_AUTH = 4
EXIT_CANCELLED = 5
EXIT_PARTIAL = 6


class TidyError(Exception):
    """Base class for all gmail-tidy errors."""


class ConfigError(TidyError):
    """Invalid configuration (maps to EXIT_CONFIG)."""


class AuthError(TidyError):
    """Authentication/authorization failure (maps to EXIT_AUTH)."""


class NoWorkError(TidyError):
    """Nothing to do (maps to EXIT_NOOP)."""


class RequestError(TidyError):
    """Gmail API request failed after retries (maps to EXIT_RUNTIME)."""


class PartialError(TidyError):
    """Some batches failed; resume with apply (maps to EXIT_PARTIAL)."""
```

- [ ] **Step 4: Install and run test to verify it passes**

Run: `python -m pip install -e ".[dev]"` then `python -m pytest tests/test_smoke.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore LICENSE src/gmail_tidy tests
git commit -m "chore: scaffold gmail-tidy package with errors and exit codes"
```

---

### Task 2: Config loading and validation

**Files:**
- Create: `src/gmail_tidy/config.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `errors.ConfigError`
- Produces:
  - `PROTECTED_LABELS: frozenset[str]`
  - `PRESETS: dict[str, dict]` (disabled by default)
  - Dataclasses `MatchConfig`, `Actions`, `Rule`, `Config` (field names exactly as used in Tasks 4/7/8).
  - `def load_config(path: Path) -> Config`
  - `def default_template() -> str`
  - `def config_dir() -> Path` (`$GMAIL_TIDY_CONFIG` or `~/.config/gmail-tidy`)
  - `def ensure_config_dir() -> Path` (mkdir, chmod 0700 on POSIX)
  - Both `match_from`/`match_label` (spec §5 example keys) and `from_contains`/`labels_have` (grammar keys) are accepted, normalized to the canonical dataclass names.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
from pathlib import Path
import pytest
from gmail_tidy.config import load_config, default_template
from gmail_tidy.errors import ConfigError


def _write(tmp_path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_valid_config(tmp_path):
    cfg = _write(tmp_path,
        "account: you@example.com\n"
        "protect:\n"
        "  exclude:\n"
        "    - match_from: [bank@example.com]\n"
        "      match_label: [IMPORTANT]\n"
        "rules:\n"
        "  - id: r1\n"
        "    match:\n"
        "      category: newsletters\n"
        "      older_than_days: 30\n"
        "    actions:\n"
        "      add_label: [Cleanup/Newsletters]\n"
        "      archive: true\n")
    cfg_obj = load_config(cfg)
    assert cfg_obj.account == "you@example.com"
    assert cfg_obj.exclude[0].from_contains == ["bank@example.com"]
    assert cfg_obj.exclude[0].labels_have == ["IMPORTANT"]
    assert cfg_obj.rules[0].id == "r1"
    assert cfg_obj.rules[0].actions.add_label == ["Cleanup/Newsletters"]
    assert cfg_obj.rules[0].actions.archive is True


def test_remove_label_rejects_protected(tmp_path):
    cfg = _write(tmp_path,
        "rules:\n"
        "  - id: bad\n"
        "    match: {category: promotions}\n"
        "    actions:\n"
        "      remove_label: [IMPORTANT]\n")
    with pytest.raises(ConfigError, match="bad"):
        load_config(cfg)


def test_remove_label_rejects_tool_labels(tmp_path):
    cfg = _write(tmp_path,
        "rules:\n"
        "  - id: bad2\n"
        "    match: {category: receipts}\n"
        "    actions:\n"
        "      remove_label: [Cleanup/Receipts]\n")
    with pytest.raises(ConfigError, match="bad2"):
        load_config(cfg)


def test_unknown_key_reports_error(tmp_path):
    cfg = _write(tmp_path,
        "rules:\n"
        "  - id: x\n"
        "    match:\n"
        "      unknown_key: 1\n")
    with pytest.raises(ConfigError):
        load_config(cfg)


def test_default_template_disables_presets():
    text = default_template()
    assert "newsletters" in text
    assert "# rules:" in text  # presets ship commented-out (disabled)


def test_both_match_key_spellings_accepted(tmp_path):
    cfg = _write(tmp_path,
        "protect:\n"
        "  exclude:\n"
        "    - from_contains: [a@example.com]\n"
        "      labels_have: [Work]\n")
    c = load_config(cfg)
    assert c.exclude[0].from_contains == ["a@example.com"]
    assert c.exclude[0].labels_have == ["Work"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement config.py**

```python
"""YAML config loading and validation.

Rules that would remove or modify protected labels fail validation (exit 2).
Presets ship disabled (commented) in the template.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

from gmail_tidy.errors import ConfigError

PROTECTED_LABELS = frozenset(
    {"IMPORTANT", "STARRED", "SPAM", "TRASH", "DRAFT", "SENT", "CHAT"}
)

PRESETS: dict[str, dict] = {
    "newsletters": {"query": "category:updates", "subject_contains": ["Newsletter", "Digest"]},
    "promotions": {"query": "category:promotions"},
    "receipts": {"query": "category:purchases", "subject_contains": ["Receipt", "Order", "Invoice"]},
    "notifications": {"query": "category:notifications"},
    "old_unread": {"older_than_days": 90, "unread": True},
    "large_messages": {"larger_than_kb": 1024},
}


class MatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str | None = None
    from_contains: list[str] = Field(
        default=[], validation_alias=AliasChoices("from_contains", "match_from")
    )
    from_ends: list[str] = []
    to_contains: list[str] = []
    subject_contains: list[str] = []
    labels_have: list[str] = Field(
        default=[], validation_alias=AliasChoices("labels_have", "match_label")
    )
    labels_missing: list[str] = []
    older_than_days: int | None = None
    newer_than_days: int | None = None
    larger_than_kb: int | None = None
    unread: bool | None = None
    query: str | None = None


class ActionsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    add_label: list[str] = []
    remove_label: list[str] = []
    archive: bool = False

    @field_validator("remove_label")
    @classmethod
    def _no_protected(cls, v: list[str]) -> list[str]:
        for name in v:
            if name in PROTECTED_LABELS or name.startswith("Cleanup/"):
                raise ValueError(f"label '{name}' is protected and cannot be removed")
        return v


class RuleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    match: MatchModel
    actions: ActionsModel


class ProtectModel(BaseModel):
    include: list[str] = []
    exclude: list[MatchModel] = []


class ConfigModel(BaseModel):
    account: str | None = None
    protect: ProtectModel = ProtectModel()
    rules: list[RuleModel] = []


@dataclass
class MatchConfig:
    category: str | None = None
    from_contains: list[str] = field(default_factory=list)
    from_ends: list[str] = field(default_factory=list)
    to_contains: list[str] = field(default_factory=list)
    subject_contains: list[str] = field(default_factory=list)
    labels_have: list[str] = field(default_factory=list)
    labels_missing: list[str] = field(default_factory=list)
    older_than_days: int | None = None
    newer_than_days: int | None = None
    larger_than_kb: int | None = None
    unread: bool | None = None
    query: str | None = None


@dataclass
class Actions:
    add_label: list[str] = field(default_factory=list)
    remove_label: list[str] = field(default_factory=list)
    archive: bool = False


@dataclass
class Rule:
    id: str
    match: MatchConfig
    actions: Actions


@dataclass
class Config:
    account: str | None = None
    include: list[str] = field(default_factory=list)
    exclude: list[MatchConfig] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)


def _m(m: MatchModel) -> MatchConfig:
    return MatchConfig(**m.model_dump())  # canonical field names


def _a(a: ActionsModel) -> Actions:
    return Actions(
        add_label=list(a.add_label),
        remove_label=list(a.remove_label),
        archive=a.archive,
    )


def load_config(path: Path) -> Config:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if raw is None:
        raw = {}
    try:
        model = ConfigModel.model_validate(raw)
    except ValidationError as exc:
        msgs = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise ConfigError(f"invalid config in {path}: {msgs}") from exc
    return Config(
        account=model.account,
        include=list(model.protect.include),
        exclude=[_m(m) for m in model.protect.exclude],
        rules=[Rule(id=r.id, match=_m(r.match), actions=_a(r.actions)) for r in model.rules],
    )


def config_dir() -> Path:
    override = os.environ.get("GMAIL_TIDY_CONFIG")
    return Path(override) if override else Path.home() / ".config" / "gmail-tidy"


def ensure_config_dir() -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            d.chmod(0o700)
        except OSError:
            pass
    return d


def default_template() -> str:
    return """# gmail-tidy configuration
# Presets are DISABLED by default: uncomment to enable.
# Rules match metadata only (never message bodies).

account: you@example.com

# Global guardrails. include: if non-empty, a message must match at least one
# query to be eligible. exclude: matching ANY rule is never touched.
protect:
  include: []
  exclude:
    # - match_from: ["bank@example.com"]
    # - match_label: ["IMPORTANT", "STARRED", "Work"]

rules:
  # - id: old-unread-newsletters
  #   match:
  #     category: newsletters
  #     older_than_days: 30
  #     unread: true
  #   actions:
  #     add_label: ["Cleanup/Newsletters"]
  #     archive: true
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/gmail_tidy/config.py tests/test_config.py
git commit -m "feat: config loading and protected-label validation"
```

---

### Task 3: MockGmailApi test double

**Files:**
- Create: `tests/mock_gmail.py`, `tests/test_mock_gmail.py`

**Interfaces:**
- Consumes: nothing
- Produces: `class MockGmailApi` exposing **only** the allowed surface via a real object graph: `users().messages().list/get/batchModify`, `users().labels().list/get/create`, `users().getProfile()`. Any other method raises `AttributeError`. State: `api.store: dict[str, _Msg]` (label_ids in `.label_ids`), `api.labels: dict[str, str]`; injection via `api.fail_before: callable|None` (called with the method name, may raise `_GError(status)`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mock_gmail.py
import pytest
from tests.mock_gmail import MockGmailApi, _GError
from gmail_tidy.gmail_client import GmailClient  # exists after Task 5


def test_pagination_roundtrip():
    api = MockGmailApi()
    for i in range(5):
        api.add_message(f"m{i}")
    assert GmailClient(api).list() == [f"m{i}" for i in range(5)]


def test_batch_modify_applies_labels():
    api = MockGmailApi()
    api.add_message("m1", labels={"INBOX"})
    GmailClient(api).batch_modify(["m1"], add=["Cleanup/A"], remove=["INBOX"])
    assert "Cleanup/A" in api.store["m1"].label_ids
    assert "INBOX" not in api.store["m1"].label_ids


def test_forbidden_attribute_raises():
    api = MockGmailApi()
    with pytest.raises(AttributeError, match="forbidden"):
        api.users().messages().trash().execute()


def test_fail_before_injection():
    api = MockGmailApi()
    api.add_message("m1")

    def _boom(method):
        if method == "list":
            raise _GError(429, "rate limit")

    api.fail_before = _boom
    with pytest.raises(_GError):
        api.users().messages().list().execute()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mock_gmail.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement the mock**

```python
# tests/mock_gmail.py
"""In-memory double for the Gmail API. Only the allowed surface exists:
messages.list/get/batchModify, labels.list/get/create, users.getProfile.
Any other callable raises AttributeError — a second safety gate behind the AST test."""

from dataclasses import dataclass, field


class _GError(Exception):
    def __init__(self, status: int, reason: str = "error"):
        self.status = status
        self.reason = reason
        super().__init__(f"HTTP {status}: {reason}")


@dataclass
class _Msg:
    id: str
    thread_id: str
    label_ids: set[str] = field(default_factory=set)
    internal_date_ms: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    size_kb: float = 0.0
    unread: bool = False


class _Req:
    """A pending request; execute() runs the handler (fail injection first)."""

    def __init__(self, api, method, kwargs):
        self._api = api
        self._method = method
        self._kwargs = kwargs

    def execute(self):
        if self._api.fail_before:
            self._api.fail_before(self._method)
        handler = self._api._handlers.get(self._method)
        if handler is None:
            raise AttributeError(f"forbidden Gmail API method: {self._method}")
        return handler(**self._kwargs)


class _Coll:
    """Resource collection, e.g. users().messages() or users().labels()."""

    def __init__(self, api, prefix: str):
        self._api = api
        self._prefix = prefix

    def __getattr__(self, name):
        method = f"{self._prefix}.{name}" if self._prefix else name
        return lambda **kw: _Req(self._api, method, kw)


class _Users:
    def __init__(self, api):
        self._api = api

    def messages(self):
        return _Coll(self._api, "")

    def labels(self):
        return _Coll(self._api, "labels")

    def getProfile(self, **kw):
        return _Req(self._api, "getProfile", kw)


class MockGmailApi:
    def __init__(self):
        self.store: dict[str, _Msg] = {}
        self.labels: dict[str, str] = {}
        self.fail_before = None
        self._handlers = {
            "list": self._list,
            "get": self._get,
            "batchModify": self._batch_modify,
            "getProfile": self._get_profile,
            "labels.list": self._labels_list,
            "labels.get": self._labels_get,
            "labels.create": self._labels_create,
        }

    def users(self):
        return _Users(self)

    # --- setup helpers -------------------------------------------------
    def add_message(self, msg_id: str, *, labels: set[str] | None = None,
                    size_kb: float = 0.0, subject: str = "",
                    from_hdr: str = "sender@example.com", to_hdr: str = "you@example.com",
                    unread: bool = False, internal_date_ms: int = 0) -> str:
        self.store[msg_id] = _Msg(
            id=msg_id,
            thread_id=f"t-{msg_id}",
            label_ids=set(labels or {"INBOX"}),
            internal_date_ms=internal_date_ms,
            headers={"From": from_hdr, "To": to_hdr, "Subject": subject},
            size_kb=size_kb,
            unread=unread,
        )
        return msg_id

    # --- handlers -----------------------------------------------------
    def _list(self, **kw):
        query = kw.get("q", "")
        page_token = kw.get("pageToken")
        page_size = 2  # fixed small size forces pagination in tests
        msgs = [m for m in self.store.values() if _matches_query(m, query)]
        start = int(page_token) if page_token else 0
        chunk = msgs[start:start + page_size]
        result = {"messages": [{"id": m.id} for m in chunk]}
        if start + page_size < len(msgs):
            result["nextPageToken"] = str(start + page_size)
        return result

    def _get(self, **params):
        m = self.store[params["id"]]
        labels = set(m.label_ids)
        if m.unread:
            labels.add("UNREAD")
        return {
            "id": m.id,
            "threadId": m.thread_id,
            "labelIds": sorted(labels),
            "internalDate": str(m.internal_date_ms),
            "sizeEstimate": int(m.size_kb * 1024),  # matches real Gmail API field
            "payload": {"headers": [{"name": k, "value": v} for k, v in m.headers.items()]},
        }

    def _batch_modify(self, **kw):
        body = kw["body"]
        for msg_id in body["ids"]:
            m = self.store[msg_id]
            for label in body.get("addLabelIds", []):
                m.label_ids.add(label)
            for label in body.get("removeLabelIds", []):
                m.label_ids.discard(label)
        return {}

    def _get_profile(self, **kw):
        return {"emailAddress": "you@example.com"}

    def _labels_list(self, **kw):
        return {"labels": [{"id": v, "name": k} for k, v in self.labels.items()]}

    def _labels_get(self, **kw):
        name = kw["id"]
        return {"id": self.labels.get(name, name), "name": name}

    def _labels_create(self, **kw):
        name = kw["body"]["name"]
        self.labels[name] = name.replace("/", "_")
        return {"id": self.labels[name], "name": name}

    def __getattr__(self, name):
        raise AttributeError(f"forbidden Gmail API method: {name}")


def _matches_query(m: _Msg, query: str) -> bool:
    if not query:
        return True
    haystack = f"{m.headers.get('From', '')} {m.headers.get('Subject', '')}".lower()
    return all(part.lower() in haystack for part in query.split() if part)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mock_gmail.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/mock_gmail.py tests/test_mock_gmail.py
git commit -m "test: in-memory MockGmailApi double with allowed-surface gate"
```

---

### Task 4: Rule evaluator (pure logic)

**Files:**
- Create: `src/gmail_tidy/rules.py`, `tests/test_rules.py`

**Interfaces:**
- Consumes: `config.MatchConfig`, `config.Actions`, `config.Rule`, `config.Config`
- Produces:
  - `@dataclass MessageMeta` fields: `id, thread_id, labels, internal_date_ms, from_header, to_header, subject_header, size_kb, unread`
  - `def matches_rule(match: MatchConfig, meta: MessageMeta) -> bool`
  - `def matches_any(matches: list[MatchConfig], meta: MessageMeta) -> bool`
  - `def is_excluded(config: Config, meta: MessageMeta) -> bool`
  - `def is_included(config: Config, meta: MessageMeta) -> bool`
  - `def first_matching_rule(config: Config, meta: MessageMeta) -> Rule | None` (None if excluded, not included, or no rule matches)

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rules.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement rules.py**

```python
"""Pure rule evaluation over message metadata. No network, no Gmail calls.

Only metadata (labels, headers, size, internalDate, unread) is evaluated —
search queries narrow the candidate set only and are never the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from gmail_tidy.config import Config, MatchConfig, Rule

PROTECTED_AT_RUNTIME = frozenset(
    {"IMPORTANT", "STARRED", "SPAM", "TRASH", "DRAFT", "SENT", "CHAT"}
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
    probes = {
        "newsletters": ["newsletter", "digest", "unsubscribe"],
        "promotions": ["promotion", "sale", "offer", "discount"],
        "receipts": ["receipt", "order", "invoice", "payment"],
        "notifications": ["notification", "alert"],
    }.get(category, [])
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rules.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/gmail_tidy/rules.py tests/test_rules.py
git commit -m "feat: pure rule evaluator with include/exclude gates"
```

---

### Task 5: Gmail client wrapper (paging, retries, batchModify)

**Files:**
- Create: `src/gmail_tidy/gmail_client.py`, `tests/test_gmail_client.py`

**Interfaces:**
- Consumes: `errors.AuthError/RequestError`, `rules.MessageMeta`, `tests.mock_gmail.MockGmailApi`
- Produces:
  - `def chunked(items: list[str], size: int = 1000) -> list[list[str]]`
  - `class GmailClient(service, page_size: int = 100)` with `list(query="", limit=None) -> list[str]`, `get_meta(msg_id) -> MessageMeta`, `batch_modify(ids, add: list[str], remove: list[str])`, `list_label_names() -> list[str]`, `ensure_label(name) -> str`, `profile_email() -> str`.
  - Retry 429/500/503 up to 3 attempts with exponential backoff (base 2s, cap 60s); raise `AuthError` on 403, `RequestError` on persistent failure.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gmail_client.py
import pytest
from tests.mock_gmail import MockGmailApi, _GError
from gmail_tidy.gmail_client import GmailClient, chunked
from gmail_tidy.errors import AuthError


def test_list_pages_all():
    api = MockGmailApi()
    for i in range(5):
        api.add_message(f"m{i}")
    assert GmailClient(api).list() == [f"m{i}" for i in range(5)]


def test_limit_respected():
    api = MockGmailApi()
    for i in range(6):
        api.add_message(f"m{i}")
    assert GmailClient(api).list(limit=3) == ["m0", "m1", "m2"]


def test_batch_modify_calls_api():
    api = MockGmailApi()
    api.add_message("m1")
    api.add_message("m2")
    GmailClient(api).batch_modify(["m1", "m2"], add=["Cleanup/A"], remove=["INBOX"])
    assert "Cleanup/A" in api.store["m1"].label_ids
    assert "INBOX" not in api.store["m2"].label_ids


def test_retry_on_429_then_success(monkeypatch):
    api = MockGmailApi()
    api.add_message("m1")
    calls = {"n": 0}
    orig = api._handlers["list"]

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _GError(429, "rate limit")
        return orig(**kw)

    api._handlers["list"] = flaky
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert GmailClient(api).list() == ["m1"]
    assert calls["n"] == 2


def test_403_raises_auth_error(monkeypatch):
    api = MockGmailApi()
    api._handlers["list"] = lambda **kw: (_ for _ in ()).throw(_GError(403, "denied"))
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(AuthError):
        GmailClient(api).list()


def test_chunked_splits_at_1000():
    assert chunked(list(range(2500)), 1000) == [
        list(range(1000)), list(range(1000, 2000)), list(range(2000, 2500))
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_gmail_client.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement gmail_client.py**

```python
"""Thin, retry-capable wrapper over the Gmail API.

Only the allowed surface is used: list/get/batchModify on messages,
list/get/create on labels, getProfile on users.
"""

from __future__ import annotations

import time

from gmail_tidy.errors import AuthError, RequestError
from gmail_tidy.rules import MessageMeta

PAGE_SIZE = 100
BATCH_SIZE = 1000
MAX_RETRIES = 3
BACKOFF_BASE = 2.0
BACKOFF_CAP = 60.0


def chunked(items: list[str], size: int = BATCH_SIZE) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


class GmailClient:
    def __init__(self, service, page_size: int = PAGE_SIZE):
        self._svc = service
        self._page_size = page_size

    def _execute(self, request, method: str):
        attempt = 0
        while True:
            try:
                return request.execute()
            except Exception as exc:
                status = getattr(exc, "status", None)
                if status in (429, 500, 503) and attempt < MAX_RETRIES:
                    attempt += 1
                    delay = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt))
                    time.sleep(delay)
                    continue
                if status == 403:
                    raise AuthError(
                        "Gmail access denied (403) — run `gmail-tidy auth` to re-authenticate"
                    ) from exc
                raise RequestError(f"Gmail request failed (status={status}) in {method}") from exc

    def list(self, query: str = "", limit: int | None = None) -> list[str]:
        out: list[str] = []
        page_token = None
        page_size = min(self._page_size, limit) if limit else self._page_size
        while True:
            params = {"userId": "me", "maxResults": page_size}
            if query:
                params["q"] = query
            if page_token:
                params["pageToken"] = page_token
            data = self._execute(self._svc.users().messages().list(**params), "list")
            out.extend(m["id"] for m in data.get("messages", []))
            if limit is not None and len(out) >= limit:
                return out[:limit]
            page_token = data.get("nextPageToken")
            if not page_token:
                return out

    def get_meta(self, msg_id: str) -> MessageMeta:
        data = self._execute(
            self._svc.users().messages().get(userId="me", id=msg_id, format="metadata"),
            "get",
        )
        headers = {h["name"]: h["value"] for h in data.get("payload", {}).get("headers", [])}
        return MessageMeta(
            id=data["id"],
            thread_id=data.get("threadId", data["id"]),
            labels=set(data.get("labelIds", [])),
            internal_date_ms=int(data.get("internalDate", "0")),
            from_header=headers.get("From"),
            to_header=headers.get("To"),
            subject_header=headers.get("Subject"),
            size_kb=data.get("sizeEstimate", 0) / 1024.0,
            unread="UNREAD" in data.get("labelIds", []),
        )

    def batch_modify(self, ids: list[str], add: list[str], remove: list[str]) -> None:
        for chunk in chunked(ids):
            body: dict = {"ids": chunk}
            if add:
                body["addLabelIds"] = add
            if remove:
                body["removeLabelIds"] = remove
            self._execute(
                self._svc.users().messages().batchModify(userId="me", body=body),
                "batchModify",
            )

    def list_label_names(self) -> list[str]:
        data = self._execute(self._svc.users().labels().list(userId="me"), "labels.list")
        return [lbl["name"] for lbl in data.get("labels", [])]

    def ensure_label(self, name: str) -> str:
        data = self._execute(self._svc.users().labels().list(userId="me"), "labels.list")
        for lbl in data.get("labels", []):
            if lbl["name"] == name:
                return lbl["id"]
        created = self._execute(
            self._svc.users().labels().create(userId="me", body={"name": name}),
            "labels.create",
        )
        return created["id"]

    def profile_email(self) -> str:
        data = self._execute(self._svc.users().getProfile(userId="me"), "getProfile")
        return data.get("emailAddress", "")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_gmail_client.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/gmail_tidy/gmail_client.py tests/test_gmail_client.py
git commit -m "feat: paged, retrying Gmail client wrapper (batchModify only)"
```

---

### Task 6: Audit log and run journal

**Files:**
- Create: `src/gmail_tidy/audit.py`, `tests/test_audit.py`

**Interfaces:**
- Consumes: `config.Actions`
- Produces:
  - `@dataclass AuditEntry` fields: `run_id, message_id, thread_id, rule_id, action, payload (str|None), kind (str, default "apply"), ts (float)`
  - `class AuditLog(path)` — `append(entry)`, `entries() -> list[AuditEntry]`; chmod 600 on POSIX.
  - `@dataclass Candidate` fields: `message_id, thread_id, rule_id, actions: Actions, before_labels: set[str], in_inbox: bool`
  - `class RunJournal(dir)` — `init_run() -> str`, `save_candidates(run_id, list[Candidate])`, `load_candidates(run_id) -> list[Candidate]`, `record_failure(run_id, message_id, err)`, `failures(run_id) -> list[str]`, `list_runs() -> list[str]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_audit.py
import json
from gmail_tidy.audit import AuditLog, AuditEntry, RunJournal, Candidate
from gmail_tidy.config import Actions


def test_audit_log_shape(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(AuditEntry(run_id="r1", message_id="m1", thread_id="t1",
                          rule_id="rule1", action="add_label", payload="Cleanup/A"))
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    # fields ONLY — never sender/subject/body/size/content
    assert set(rec) == {"ts", "run_id", "message_id", "thread_id", "rule_id", "action", "payload", "kind"}
    assert "sender" not in json.dumps(rec).lower()


def test_journal_roundtrip_and_failures(tmp_path):
    j = RunJournal(tmp_path / "runs")
    run_id = j.init_run()
    cand = Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(add_label=["Cleanup/A"], archive=True),
                     before_labels={"INBOX"}, in_inbox=True)
    j.save_candidates(run_id, [cand])
    loaded = j.load_candidates(run_id)
    assert loaded == [cand]
    assert loaded[0].actions.add_label == ["Cleanup/A"]
    j.record_failure(run_id, "m1", "rate limited")
    assert j.failures(run_id) == ["m1: rate limited"]
    assert run_id in j.list_runs()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_audit.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement audit.py**

```python
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

    def record_failure(self, run_id: str, message_id: str, err: str) -> None:
        path = self.dir / f"{run_id}.failures.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"message_id": message_id, "err": err}) + "\n")

    def failures(self, run_id: str) -> list[str]:
        path = self.dir / f"{run_id}.failures.jsonl"
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as fh:
            return [f"{json.loads(line)['message_id']}: {json.loads(line)['err']}" for line in fh]

    def list_runs(self) -> list[str]:
        if not self.dir.exists():
            return []
        files = [p for p in self.dir.glob("*.json")]
        files.sort(key=lambda p: p.stat().st_mtime)  # chronological, oldest first
        return [p.stem for p in files]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_audit.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/gmail_tidy/audit.py tests/test_audit.py
git commit -m "feat: durable audit log + resumable run journal"
```

---

### Task 7: Scan and apply (planning + reconcile-before-apply)

**Files:**
- Create: `src/gmail_tidy/actions.py`, `tests/test_actions.py`

**Interfaces:**
- Consumes: `config.Config`, `rules` (`first_matching_rule`, `is_excluded`, `MessageMeta`), `GmailClient`, `RunJournal`, `AuditLog`, `AuditEntry`, `Actions`, errors (`EXIT_OK`, `EXIT_CANCELLED`, `EXIT_PARTIAL`)
- Produces:
  - `def query_from_match(match: MatchConfig) -> str`
  - `def noop_eliminate(meta: MessageMeta, actions: Actions) -> tuple[Actions, bool]` — drop actions already true against `meta.labels`; returns `(fresh, changed)`.
  - `def scan(client: GmailClient, config: Config, limit: int | None = None) -> list[Candidate]`
  - `def apply_run(client: GmailClient, config: Config, candidates: list[Candidate], journal: RunJournal, audit: AuditLog, run_id: str, confirm: Callable[[], bool]) -> int`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_actions.py
from pathlib import Path
from gmail_tidy.config import Config, Rule, MatchConfig, Actions
from gmail_tidy.actions import scan, apply_run
from gmail_tidy.audit import RunJournal, AuditLog, Candidate
from gmail_tidy.gmail_client import GmailClient
from gmail_tidy.errors import EXIT_OK, EXIT_CANCELLED
from tests.mock_gmail import MockGmailApi


def _config():
    return Config(
        rules=[
            Rule(id="r1", match=MatchConfig(subject_contains=["newsletter"], older_than_days=10),
                 actions=Actions(add_label=["Cleanup/N"], archive=True)),
        ]
    )


def test_scan_builds_candidates():
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    api.add_message("m2", subject="receipt", labels={"INBOX"})
    c = scan(GmailClient(api), _config())
    assert [x.message_id for x in c] == ["m1"]
    assert c[0].actions.add_label == ["Cleanup/N"]
    assert c[0].in_inbox is True
    assert c[0].before_labels == {"INBOX"}


def test_apply_skips_newly_excluded_message(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", subject="news", labels={"IMPORTANT"})  # protected now
    client = GmailClient(api)
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(add_label=["Cleanup/N"], archive=True),
                     before_labels={"INBOX"}, in_inbox=True)
    j.save_candidates(run_id, [cand])
    apply_run(client, _config(), [cand], j, audit, run_id, confirm=lambda: True)
    assert "Cleanup/N" not in api.store["m1"].label_ids
    assert not audit.entries()


def test_apply_audits_each_action(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", subject="news", labels={"INBOX"})
    client = GmailClient(api)
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(add_label=["Cleanup/N"], archive=True),
                     before_labels={"INBOX"}, in_inbox=True)
    j.save_candidates(run_id, [cand])
    result = apply_run(client, _config(), [cand], j, audit, run_id, confirm=lambda: True)
    assert result == EXIT_OK
    assert len(audit.entries()) == 2  # add_label + archive
    assert "Cleanup/N" in api.store["m1"].label_ids
    assert "INBOX" not in api.store["m1"].label_ids


def test_apply_cancel_is_exit_5(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", subject="news", labels={"INBOX"})
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(archive=True), before_labels={"INBOX"}, in_inbox=True)
    result = apply_run(GmailClient(api), _config(), [cand], j, audit, run_id, confirm=lambda: False)
    assert result == EXIT_CANCELLED
    assert not audit.entries()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_actions.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement actions.py**

```python
"""Planning (scan) and the only write path (apply). Reconcile-before-apply."""

from __future__ import annotations

from collections.abc import Callable

from gmail_tidy.audit import AuditEntry, AuditLog, Candidate, RunJournal
from gmail_tidy.config import Actions, Config, MatchConfig
from gmail_tidy.errors import EXIT_CANCELLED, EXIT_OK, EXIT_PARTIAL
from gmail_tidy.gmail_client import GmailClient
from gmail_tidy.rules import MessageMeta, first_matching_rule, is_excluded


def query_from_match(match: MatchConfig) -> str:
    """Best-effort Gmail search narrowing. Never the source of truth."""
    parts: list[str] = []
    if match.category:
        parts.append(f"category:{match.category}")
    for s in match.subject_contains:
        parts.append(f'subject:"{s}"')
    for s in match.from_contains:
        parts.append(f'from:"{s}"')
    if match.older_than_days:
        parts.append(f"older_than:{match.older_than_days}d")
    if match.unread is True:
        parts.append("is:unread")
    return " ".join(parts)


def noop_eliminate(meta: MessageMeta, actions: Actions) -> tuple[Actions, bool]:
    """Drop actions already satisfied by the message's current state."""
    add = [l for l in actions.add_label if l not in meta.labels]
    remove = [l for l in actions.remove_label if l in meta.labels]
    archive = actions.archive and "INBOX" in meta.labels
    changed = bool(add or remove or archive)
    return Actions(add_label=add, remove_label=remove, archive=archive), changed


def scan(client: GmailClient, config: Config, limit: int | None = None) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for rule in config.rules:
        ids = client.list(query_from_match(rule.match), limit=limit)
        for msg_id in ids:
            if msg_id in seen:
                continue
            seen.add(msg_id)
            meta = client.get_meta(msg_id)
            matched = first_matching_rule(config, meta)
            if matched is None or matched.id != rule.id:
                continue  # another rule won, or message excluded/not included
            actions, changed = noop_eliminate(meta, rule.actions)
            if not changed:
                continue
            candidates.append(Candidate(
                message_id=meta.id,
                thread_id=meta.thread_id,
                rule_id=rule.id,
                actions=actions,
                before_labels=set(meta.labels),
                in_inbox="INBOX" in meta.labels,
            ))
    return candidates


def apply_run(client: GmailClient, config: Config, candidates: list[Candidate],
              journal: RunJournal, audit: AuditLog, run_id: str,
              confirm: Callable[[], bool]) -> int:
    """Re-verify every candidate against current state, then write. Returns exit code."""
    if not confirm():
        return EXIT_CANCELLED
    failed = 0
    for cand in candidates:
        try:
            meta = client.get_meta(cand.message_id)
        except Exception:
            journal.record_failure(run_id, cand.message_id, "message gone or unreadable")
            failed += 1
            continue
        if is_excluded(config, meta):
            continue
        fresh, changed = noop_eliminate(meta, cand.actions)
        if not changed:
            continue
        try:
            client.batch_modify([meta.id], add=fresh.add_label, remove=fresh.remove_label)
        except Exception as exc:
            journal.record_failure(run_id, cand.message_id, str(exc))
            failed += 1
            continue
        for label in fresh.add_label:
            audit.append(AuditEntry(run_id=run_id, message_id=meta.id, thread_id=meta.thread_id,
                                    rule_id=cand.rule_id, action="add_label", payload=label))
        for label in fresh.remove_label:
            audit.append(AuditEntry(run_id=run_id, message_id=meta.id, thread_id=meta.thread_id,
                                    rule_id=cand.rule_id, action="remove_label", payload=label))
        if fresh.archive:
            audit.append(AuditEntry(run_id=run_id, message_id=meta.id, thread_id=meta.thread_id,
                                    rule_id=cand.rule_id, action="archive", payload="INBOX"))
    return EXIT_PARTIAL if failed else EXIT_OK
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_actions.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/gmail_tidy/actions.py tests/test_actions.py
git commit -m "feat: scan planning and reconcile-before-apply write path"
```

---

### Task 8: Undo

**Files:**
- Create: `src/gmail_tidy/undo.py`, `tests/test_undo.py`

**Interfaces:**
- Consumes: `audit.Candidate`, `audit.RunJournal`, `audit.AuditLog`, `audit.AuditEntry`, `GmailClient`, errors (`EXIT_OK`, `EXIT_CANCELLED`)
- Produces:
  - `@dataclass InverseAction` fields: `message_id, thread_id, rule_id, add_label: list[str], remove_label: list[str], re_inbox: bool, expected_labels: set[str]`
  - `def expected_after(cand: Candidate) -> set[str]` — labels the run should have left behind (`before_labels ∪ add − remove − {INBOX if archived}`).
  - `def build_undo_plan(cand: Candidate) -> list[InverseAction]` — inverse: re-add what was removed, remove what was added, re-add INBOX.
  - `def execute_undo(client: GmailClient, plan: list[InverseAction], audit: AuditLog, run_id: str, confirm: Callable[[], bool]) -> int` — skips any message whose current labels ≠ `expected_labels` (the user-changed guard); idempotent; audits with `kind="undo"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_undo.py
from pathlib import Path
from gmail_tidy.undo import build_undo_plan, execute_undo
from gmail_tidy.audit import Candidate, RunJournal, AuditLog
from gmail_tidy.config import Actions
from gmail_tidy.gmail_client import GmailClient
from gmail_tidy.errors import EXIT_OK
from tests.mock_gmail import MockGmailApi


def _cand() -> Candidate:
    return Candidate(message_id="m1", thread_id="t1", rule_id="r1",
                     actions=Actions(add_label=["Cleanup/N"], archive=True),
                     before_labels={"INBOX"}, in_inbox=True)


def test_undo_skips_user_changed_message(tmp_path):
    api = MockGmailApi()
    # apply left: INBOX removed + Cleanup/N added; user then added B manually
    api.add_message("m1", labels={"Cleanup/N", "B"})
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = _cand()
    j.save_candidates(run_id, [cand])
    plan = build_undo_plan(cand)
    result = execute_undo(GmailClient(api), plan, audit, run_id, confirm=lambda: True)
    assert result == EXIT_OK
    # user label B untouched; INBOX NOT re-added (message was user-changed)
    assert "B" in api.store["m1"].label_ids
    assert "INBOX" not in api.store["m1"].label_ids
    assert not audit.entries()


def test_undo_restores_when_unchanged(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})  # matches left-behind state
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = _cand()
    j.save_candidates(run_id, [cand])
    plan = build_undo_plan(cand)
    result = execute_undo(GmailClient(api), plan, audit, run_id, confirm=lambda: True)
    assert result == EXIT_OK
    assert "INBOX" in api.store["m1"].label_ids
    assert "Cleanup/N" not in api.store["m1"].label_ids
    entries = audit.entries()
    assert entries and entries[0].kind == "undo"


def test_undo_is_idempotent(tmp_path):
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    j = RunJournal(tmp_path / "runs")
    audit = AuditLog(tmp_path / "audit.jsonl")
    run_id = j.init_run()
    cand = _cand()
    j.save_candidates(run_id, [cand])
    plan = build_undo_plan(cand)
    execute_undo(GmailClient(api), plan, audit, run_id, confirm=lambda: True)
    execute_undo(GmailClient(api), plan, audit, run_id, confirm=lambda: True)
    # second run: state no longer matches left-behind → skipped, no new entries
    assert "INBOX" in api.store["m1"].label_ids
    assert len(audit.entries()) == 2  # add_label + archive from the first undo only
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_undo.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement undo.py**

```python
"""Undo reverses a run's label/archive actions, skipping user-changed messages.

The safety rule (spec §11): a message is only touched if its current label set
exactly equals the state the run left behind. If the user changed anything since,
the message is skipped — newer user state is never clobbered.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from gmail_tidy.audit import AuditEntry, AuditLog, Candidate
from gmail_tidy.errors import EXIT_CANCELLED, EXIT_OK
from gmail_tidy.gmail_client import GmailClient


@dataclass
class InverseAction:
    message_id: str
    thread_id: str
    rule_id: str
    add_label: list[str] = field(default_factory=list)
    remove_label: list[str] = field(default_factory=list)
    re_inbox: bool = False
    expected_labels: set[str] = field(default_factory=set)


def expected_after(cand: Candidate) -> set[str]:
    """Labels the run should have left behind."""
    removed = set(cand.actions.remove_label)
    if cand.actions.archive:
        removed.add("INBOX")
    return (set(cand.before_labels) | set(cand.actions.add_label)) - removed


def build_undo_plan(cand: Candidate) -> list[InverseAction]:
    """Invert the run: re-add what it removed, remove what it added, re-inbox."""
    return [
        InverseAction(
            message_id=cand.message_id,
            thread_id=cand.thread_id,
            rule_id=cand.rule_id,
            add_label=list(cand.actions.remove_label),
            remove_label=list(cand.actions.add_label),
            re_inbox=cand.actions.archive and cand.in_inbox,
            expected_labels=expected_after(cand),
        )
    ]


def execute_undo(client: GmailClient, plan: list[InverseAction], audit: AuditLog,
                 run_id: str, confirm: Callable[[], bool]) -> int:
    if not confirm():
        return EXIT_CANCELLED
    for inv in plan:
        meta = client.get_meta(inv.message_id)
        if set(meta.labels) != inv.expected_labels:
            continue  # user changed the message; never clobber
        add = list(inv.add_label) + (["INBOX"] if inv.re_inbox else [])
        remove = list(inv.remove_label)
        if not add and not remove:
            continue
        client.batch_modify([meta.id], add=add, remove=remove)
        for label in add:
            audit.append(AuditEntry(run_id=run_id, message_id=meta.id, thread_id=meta.thread_id,
                                    rule_id=inv.rule_id, action="add_label", payload=label,
                                    kind="undo"))
        for label in remove:
            audit.append(AuditEntry(run_id=run_id, message_id=meta.id, thread_id=meta.thread_id,
                                    rule_id=inv.rule_id, action="remove_label", payload=label,
                                    kind="undo"))
    return EXIT_OK
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_undo.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/gmail_tidy/undo.py tests/test_undo.py
git commit -m "feat: undo with user-change safety guard"
```

---

### Task 9: Auth (OAuth2, scopes, revoke)

**Files:**
- Create: `src/gmail_tidy/auth.py`, `tests/test_auth.py`

**Interfaces:**
- Consumes: `errors.AuthError`
- Produces:
  - `SCOPE_READONLY`, `SCOPE_MODIFY`, `SCOPE_LABELS`, `SCOPE_WRITE = [SCOPE_MODIFY, SCOPE_LABELS]`
  - `def scope_state(token_path: Path) -> set[str]`
  - `def get_credentials(cfg: Path, client_secret: Path, require_write: bool = False) -> Credentials` — loads/saves `token.json` (chmod 600) recording scopes; raises `AuthError` when secret missing or consent fails.
  - `def upgrade_write(cfg: Path, client_secret: Path) -> Credentials` — fresh consent with write scopes, replacing the token.
  - `def revoke(cfg: Path) -> None` — best-effort server revoke, then delete `token.json`; never touches config/audit.
  - `def token_path(cfg: Path) -> Path`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_auth.py
from pathlib import Path
from gmail_tidy.auth import scope_state, revoke, token_path, SCOPE_READONLY


def test_scope_state_reads_metadata(tmp_path):
    tok = tmp_path / "token.json"
    tok.write_text('{"scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}',
                   encoding="utf-8")
    assert scope_state(tok) == {SCOPE_READONLY}


def test_scope_state_empty_when_missing(tmp_path):
    assert scope_state(tmp_path / "nope.json") == set()


def test_revoke_removes_local_files_never_audit(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "token.json").write_text("{}", encoding="utf-8")
    (cfg / "audit.jsonl").write_text("x\n", encoding="utf-8")
    (cfg / "config.yaml").write_text("account: x", encoding="utf-8")
    revoke(cfg)
    assert not (cfg / "token.json").exists()
    assert (cfg / "audit.jsonl").exists()
    assert (cfg / "config.yaml").exists()


def test_token_path_constant(tmp_path):
    assert token_path(tmp_path) == tmp_path / "token.json"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_auth.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement auth.py**

```python
"""OAuth2 with the Gmail API.

Read-only scope by default; write scopes (gmail.modify + gmail.labels) are
requested only when apply/undo actually need them. Revoke removes the local
token after a best-effort server revoke; config and audit log are never touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from gmail_tidy.errors import AuthError

SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
SCOPE_LABELS = "https://www.googleapis.com/auth/gmail.labels"
SCOPE_WRITE = [SCOPE_MODIFY, SCOPE_LABELS]

TOKEN_NAME = "token.json"
SECRET_NAME = "client_secret.json"


def token_path(cfg: Path) -> Path:
    return cfg / TOKEN_NAME


def scope_state(token: Path) -> set[str]:
    if not token.exists():
        return set()
    try:
        data = json.loads(token.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return set(data.get("scopes", []))


def _chmod_600(path: Path) -> None:
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _save_token(cfg: Path, creds: Credentials, scopes: list[str]) -> None:
    token = token_path(cfg)
    payload = json.loads(creds.to_json())
    payload["scopes"] = list(scopes)
    token.write_text(json.dumps(payload), encoding="utf-8")
    _chmod_600(token)


def get_credentials(cfg: Path, client_secret: Path, require_write: bool = False) -> Credentials:
    scopes = SCOPE_WRITE if require_write else [SCOPE_READONLY]
    token = token_path(cfg)
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), scopes=scopes)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            return creds
        token.unlink(missing_ok=True)  # corrupt/expired; re-consent
    if not client_secret.exists():
        raise AuthError(
            f"missing {client_secret.name} in {cfg} — see docs/google-cloud-setup.md "
            "and run `gmail-tidy auth` to re-authenticate"
        )
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), scopes=scopes)
        creds = flow.run_local_server(port=0, prompt="consent")
    except Exception as exc:
        raise AuthError(f"OAuth consent failed — run `gmail-tidy auth` to retry: {exc}") from exc
    _save_token(cfg, creds, scopes)
    return creds


def upgrade_write(cfg: Path, client_secret: Path) -> Credentials:
    token = token_path(cfg)
    token.unlink(missing_ok=True)
    return get_credentials(cfg, client_secret, require_write=True)


def revoke(cfg: Path) -> None:
    token = token_path(cfg)
    if not token.exists():
        return
    try:
        creds = Credentials.from_authorized_user_file(str(token))
        if creds and creds.refresh_token:
            creds.revoke(Request())
    except Exception:
        pass  # server unreachable: token still removed locally; note server-side lifetime
    token.unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_auth.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/gmail_tidy/auth.py tests/test_auth.py
git commit -m "feat: OAuth2 flow with read-only default and safe revoke"
```

---

### Task 10: CLI commands

**Files:**
- Create: `src/gmail_tidy/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: all modules; `config_dir/ensure_config_dir/load_config/default_template`, `GmailClient`, `scan/apply_run`, `undo.build_undo_plan/execute_undo`, `RunJournal/AuditLog`, `auth.get_credentials/revoke/upgrade_write/scope_state`, errors + exit codes.
- Produces: typer `app` with commands `init`, `scan`, `preview`, `apply`, `undo`, `status`, and sub-app `auth` (`status`, `refresh`, `revoke`). Two module-level hooks tests monkeypatch: `build_service(creds)` and `get_credentials(cfg_dir, require_write)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli.py
import pytest
from typer.testing import CliRunner
from gmail_tidy import cli
from gmail_tidy.cli import app
from tests.mock_gmail import MockGmailApi

runner = CliRunner()


def _config_text() -> str:
    return (
        "rules:\n"
        "  - id: r1\n"
        "    match: {subject_contains: [newsletter]}\n"
        "    actions:\n"
        "      add_label: [Cleanup/N]\n"
        "      archive: true\n"
    )


def test_status_exit_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "config" in result.output.lower()


def test_scan_no_auth_exits_4(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 4
    assert "auth" in result.output.lower()


def test_scan_and_preview_never_write(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_config_text(), encoding="utf-8")
    api = MockGmailApi()
    api.add_message("m1", subject="newsletter", labels={"INBOX"})
    monkeypatch.setattr(cli, "get_credentials", lambda cfg, require: object())
    monkeypatch.setattr(cli, "build_service", lambda creds: api)
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 0
    result2 = runner.invoke(app, ["preview"])
    assert result2.exit_code == 0
    # mailbox unchanged after scan + preview
    assert "Cleanup/N" not in api.store["m1"].label_ids
    assert "INBOX" in api.store["m1"].label_ids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement cli.py**

```python
"""Typer command surface.

All commands talk to Gmail by design; write commands require confirmation
(--yes bypasses); preview/undo default to dry-run (no writes).
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from gmail_tidy import audit as audit_mod
from gmail_tidy import auth as auth_mod
from gmail_tidy import config as config_mod
from gmail_tidy.actions import apply_run, scan as build_scan  # alias: command named scan below
from gmail_tidy.errors import (
    AuthError, ConfigError, NoWorkError,
    EXIT_OK, EXIT_RUNTIME, EXIT_CONFIG, EXIT_NOOP, EXIT_AUTH, EXIT_CANCELLED,
)
from gmail_tidy.gmail_client import GmailClient
from gmail_tidy.undo import build_undo_plan, execute_undo

app = typer.Typer(add_completion=False)
console = Console()


def build_service(creds):
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=creds)


def get_credentials(cfg_dir: Path, require_write: bool):
    return auth_mod.get_credentials(cfg_dir, cfg_dir / "client_secret.json", require_write=require_write)


def _load_config() -> tuple[Path, config_mod.Config]:
    cfg_dir = config_mod.config_dir()
    path = cfg_dir / "config.yaml"
    if not path.exists():
        raise ConfigError(f"no config at {path} — run `gmail-tidy init`")
    return cfg_dir, config_mod.load_config(path)


def _client(cfg_dir: Path, require_write: bool) -> GmailClient:
    return GmailClient(build_service(get_credentials(cfg_dir, require_write)))


def _latest_run(journal: audit_mod.RunJournal) -> str | None:
    runs = journal.list_runs()
    return runs[-1] if runs else None


def _exit_for(err: Exception) -> int:
    if isinstance(err, ConfigError):
        return EXIT_CONFIG
    if isinstance(err, AuthError):
        return EXIT_AUTH
    if isinstance(err, NoWorkError):
        return EXIT_NOOP
    return EXIT_RUNTIME


@app.command()
def init():
    """Create the config dir + template and start read-only OAuth."""
    try:
        cfg_dir = config_mod.ensure_config_dir()
        conf = cfg_dir / "config.yaml"
        if not conf.exists():
            conf.write_text(config_mod.default_template(), encoding="utf-8")
            console.print(f"[green]wrote[/green] {conf}")
        get_credentials(cfg_dir, require_write=False)
        console.print("[green]authenticated with read-only scope.[/green]")
        raise typer.Exit(EXIT_OK)
    except AuthError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_AUTH)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_CONFIG)


@app.command()
def scan(limit: int | None = typer.Option(None, "--limit"),
         rules: list[str] = typer.Option(None, "--rules")):
    """Build a candidate plan (read-only) and write it to the run journal."""
    try:
        cfg_dir, cfg = _load_config()
        if rules:
            cfg.rules = [r for r in cfg.rules if r.id in rules]
        client = _client(cfg_dir, require_write=False)
        candidates = build_scan(client, cfg, limit=limit)
        if not candidates:
            console.print("nothing matched the configured rules.")
            raise typer.Exit(EXIT_NOOP)
        journal = audit_mod.RunJournal(cfg_dir / "runs")
        run_id = journal.init_run()
        journal.save_candidates(run_id, candidates)
        console.print(f"[green]scan complete[/green]: {len(candidates)} candidate(s) — run {run_id}")
        raise typer.Exit(EXIT_OK)
    except (ConfigError, AuthError, NoWorkError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(_exit_for(e))


@app.command()
def preview(run: str | None = typer.Option(None, "--run")):
    """Render a run's proposed actions (dry-run, no writes)."""
    try:
        cfg_dir, _cfg = _load_config()
        journal = audit_mod.RunJournal(cfg_dir / "runs")
        run_id = run or _latest_run(journal)
        if not run_id:
            console.print("no run found — run `gmail-tidy scan` first.")
            raise typer.Exit(EXIT_NOOP)
        candidates = journal.load_candidates(run_id)
        table = Table(title=f"Run {run_id} — proposed actions (dry-run)")
        table.add_column("id")
        table.add_column("rule")
        table.add_column("actions")
        for c in candidates:
            acts: list[str] = []
            if c.actions.add_label:
                acts.append("+".join(c.actions.add_label))
            if c.actions.remove_label:
                acts.append("-".join(c.actions.remove_label))
            if c.actions.archive:
                acts.append("archive")
            table.add_row(c.message_id, c.rule_id, ", ".join(acts))
        console.print(table)
        console.print(f"[dim]{len(candidates)} message(s). Apply with `gmail-tidy apply --yes`.[/dim]")
        raise typer.Exit(EXIT_OK)
    except (ConfigError, AuthError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(_exit_for(e))


@app.command()
def apply(run_id: str | None = typer.Option(None, "--run"),
          yes: bool = typer.Option(False, "--yes")):
    """Re-verify and execute a run's actions (the only write command)."""
    try:
        cfg_dir, cfg = _load_config()
        journal = audit_mod.RunJournal(cfg_dir / "runs")
        run_id = run_id or _latest_run(journal)
        if not run_id:
            console.print("no run found — run `gmail-tidy scan` first.")
            raise typer.Exit(EXIT_NOOP)
        candidates = journal.load_candidates(run_id)
        if not candidates:
            console.print("run has no candidates.")
            raise typer.Exit(EXIT_NOOP)
        client = _client(cfg_dir, require_write=True)  # escalate scope before any write
        audit = audit_mod.AuditLog(cfg_dir / "audit.jsonl")
        console.print(f"[yellow]{len(candidates)} message(s) will be modified.[/yellow]")
        confirm = (lambda: True) if yes else (lambda: typer.confirm("Proceed with apply?"))
        result = apply_run(client, cfg, candidates, journal, audit, run_id, confirm)
        if result == EXIT_CANCELLED:
            console.print("cancelled.")
        raise typer.Exit(result)
    except (ConfigError, AuthError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(_exit_for(e))


@app.command()
def undo(run_id: str,
         yes: bool = typer.Option(False, "--yes"),
         dry_run: bool = typer.Option(False, "--dry-run")):
    """Reverse a run's actions; dry-run by default, idempotent."""
    try:
        cfg_dir = config_mod.config_dir()
        journal = audit_mod.RunJournal(cfg_dir / "runs")
        candidates = journal.load_candidates(run_id)
        plan = [inv for c in candidates for inv in build_undo_plan(c)]
        if dry_run or not yes:
            console.print(f"inverse plan for run {run_id} (dry-run):")
            for inv in plan:
                console.print(f"  {inv.message_id}: +{inv.add_label} -{inv.remove_label} "
                              f"inbox={inv.re_inbox}")
            raise typer.Exit(EXIT_OK)
        client = _client(cfg_dir, require_write=True)
        audit = audit_mod.AuditLog(cfg_dir / "audit.jsonl")
        confirm = (lambda: True) if yes else (lambda: typer.confirm("Proceed with undo?"))
        result = execute_undo(client, plan, audit, run_id, confirm)
        if result == EXIT_CANCELLED:
            console.print("cancelled.")
        raise typer.Exit(result)
    except (ConfigError, AuthError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(_exit_for(e))
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_CONFIG)


@app.command()
def status():
    """Account, scopes, run history, audit-log path."""
    try:
        cfg_dir = config_mod.config_dir()
        conf = cfg_dir / "config.yaml"
        token = cfg_dir / "token.json"
        scopes = auth_mod.scope_state(token)
        runs = audit_mod.RunJournal(cfg_dir / "runs").list_runs()
        console.print(f"config dir : {cfg_dir}")
        console.print(f"config     : {'present' if conf.exists() else 'MISSING'}")
        console.print(f"token      : {'present' if token.exists() else 'absent'}")
        console.print(f"scopes     : {sorted(scopes) or '(none)'}")
        console.print(f"last run   : {runs[-1] if runs else '(none)'}")
        console.print(f"run count  : {len(runs)}")
        console.print(f"audit log  : {cfg_dir / 'audit.jsonl'}")
        raise typer.Exit(EXIT_OK)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_CONFIG)


auth_app = typer.Typer(help="Manage OAuth tokens and scopes.")
app.add_typer(auth_app, name="auth")


@auth_app.command("status")
def auth_status():
    try:
        cfg_dir = config_mod.config_dir()
        scopes = auth_mod.scope_state(cfg_dir / "token.json")
        console.print(f"token  : {cfg_dir / 'token.json'}")
        console.print(f"scopes : {sorted(scopes) or '(none)'}")
        raise typer.Exit(EXIT_OK)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_CONFIG)


@auth_app.command("refresh")
def auth_refresh():
    try:
        cfg_dir = config_mod.config_dir()
        auth_mod.upgrade_write(cfg_dir, cfg_dir / "client_secret.json")
        console.print("[green]token refreshed with write scopes.[/green]")
        raise typer.Exit(EXIT_OK)
    except AuthError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_AUTH)


@auth_app.command("revoke")
def auth_revoke():
    try:
        cfg_dir = config_mod.config_dir()
        auth_mod.revoke(cfg_dir)
        console.print("[green]local token removed. Server-side token remains until it expires.[/green]")
        raise typer.Exit(EXIT_OK)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_CONFIG)
```

Note: `cli.py` references `EXIT_RUNTIME` in `_exit_for` — it must be imported from `gmail_tidy.errors` (add it to the import list; the import block above intentionally includes it via `from gmail_tidy.errors import (AuthError, ConfigError, NoWorkError, EXIT_OK, EXIT_CONFIG, EXIT_NOOP, EXIT_AUTH, EXIT_CANCELLED, EXIT_RUNTIME)`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/gmail_tidy/cli.py tests/test_cli.py
git commit -m "feat: CLI with exit-code contract and dry-run defaults"
```

---

### Task 11: Forbidden-API AST test

**Files:**
- Create: `tests/test_forbidden_api.py`

**Interfaces:**
- Consumes: nothing (static analysis over `src/`).
- Produces: `_find_forbidden(source: str) -> list[list[str]]` (test-local helper) proving no disallowed Gmail method/resource is referenced.

- [ ] **Step 1: Write the test file (this is the deliverable)**

```python
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
```

- [ ] **Step 2: Run to verify the helper behaves**

Run: `python -m pytest tests/test_forbidden_api.py -v`
Expected: PASS (4 passed). If `test_real_source_has_no_forbidden_calls` flags anything, refactor the flagged `src/` call so the method is never invoked on a Gmail resource.

- [ ] **Step 3: Run the whole suite**

Run: `python -m pytest -v`
Expected: ALL PASS (all tasks' tests green).

- [ ] **Step 4: Commit**

```bash
git add tests/test_forbidden_api.py
git commit -m "test: AST-based forbidden-Gmail-API gate"
```

---

### Task 12: CI and public docs

**Files:**
- Create: `.github/workflows/ci.yml`, `README.md`, `SECURITY.md`, `docs/google-cloud-setup.md`, `docs/config-reference.md`, `docs/safety-and-privacy.md`

**Interfaces:**
- Produces: repo-wide docs + CI. No package code.

- [ ] **Step 1: Write CI workflow**

`.github/workflows/ci.yml`:

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: python -m pip install -e ".[dev]"
      - run: python -m pytest -v
      - name: secret scan (no OAuth files in tree)
        run: |
          if git ls-files | grep -Ei '(^|/)(token|client_secret).*\.json$'; then
            echo "forbidden credential file in tree"; exit 1
          fi
```

- [ ] **Step 2: Write the docs with the following concrete content**

- `README.md`: 3-line pitch (declarative rules over existing mail; label+archive only; never deletes); badges; install (`pip install gmail-tidy`); quick start (Cloud setup → `gmail-tidy init` → edit config → `scan` → `preview` → `apply --yes` → `undo`); the safety model in 4 bullets; a "What this tool will never do" section listing delete/trash/spam/send/import; the CLI table (commands + exit codes from spec §4); links to the three docs.
- `SECURITY.md`: responsible-disclosure contact (repo issues/private), the allowed-write-surface statement, the no-personal-data policy (fixtures/docs use `example.com` only), the secrets policy (`client_secret*.json`/`token.json` never committed; `.gitignore` always carries them; GitHub secret scanning enabled).
- `docs/google-cloud-setup.md`: step-by-step — create Google Cloud project → enable Gmail API → OAuth consent screen ("Internal" for personal, "Testing" + test users otherwise) → create OAuth Client ID (Desktop app type) → scopes `gmail.readonly` (scan/preview) and `gmail.modify` + `gmail.labels` (apply/undo) → download `client_secret.json` into the config dir → note the consent screen explicitly lists broadened scopes on escalation.
- `docs/config-reference.md`: table of every `match` key + every `actions` key with types, examples, and the AND/OR semantics; the `match_from`/`match_label` aliases; presets table (disabled-by-default); the protected-label list; a full worked example.
- `docs/safety-and-privacy.md`: the eight invariants from spec §6; failure/backoff behavior §7; undo contract §10 with the user-change guard; audit-log field whitelist with a JSONL example.

- [ ] **Step 3: Run local CI-equivalent**

Run: `python -m pytest -q` → PASS; `git ls-files | grep -Ei '(^|/)(token|client_secret).*\.json$'` → no output.

- [ ] **Step 4: Commit**

```bash
git add .github docs README.md SECURITY.md
git commit -m "docs: CI, SECURITY, and setup reference"
```

---

## Self-Review (run against the spec before executing)

1. **§1/§2**: zero-destructive posture enforced by Task 11 AST test + Task 3 mock gate. ✓
2. **§3**: plan/play split — `scan`/`preview` never write (Task 7/10); OAuth scopes (Task 9); scope escalation on `apply`/`undo` (Task 10 `require_write=True`). ✓
3. **§4**: all commands + exit codes (Task 10 + `errors.py`). ✓
4. **§5**: schema, aliases, protected-label validation (Task 2). ✓
5. **§6**: all 8 invariants — AST test (11), reconcile-before-apply (7), no-op elimination (7), pre-apply snapshot (6/7), audit whitelist test (6), chmod-600 (6/9), confirmation (7/10), explicit allowed surface (5/11). ✓
6. **§7**: backoff/retry (5), 403→exit 4 (5/10), malformed config exit 2 (2). ✓
7. **§10 undo**: rebuild-from-snapshot + user-change skip + idempotence (Task 8). ✓
8. **§11**: offline tests throughout; `--live` explicitly excluded from CI (Task 12); undo safety test present. ✓
9. **§12**: SECURITY.md, docs, .gitignore, CI secret scan (Tasks 1/12). ✓
