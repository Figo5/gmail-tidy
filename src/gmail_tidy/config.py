"""YAML config loading and validation.

Rules that would remove or modify protected labels fail validation (exit 2).
Adding a system label (INBOX, UNREAD, ...) is also rejected at load time.
Presets ship disabled (commented) in the template.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

from gmail_tidy.errors import ConfigError
from gmail_tidy.labels import SYSTEM_LABELS

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

# Single source of truth for valid MatchModel.category values.
CATEGORIES: tuple[str, ...] = tuple(PRESETS)


class MatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str | None = None

    @field_validator("category")
    @classmethod
    def _category_must_be_preset(cls, v: str | None) -> str | None:
        if v is not None and v not in CATEGORIES:
            raise ValueError(
                f"category '{v}' is not a valid preset; expected one of: "
                f"{', '.join(CATEGORIES)}"
            )
        return v
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

    @field_validator("add_label")
    @classmethod
    def _no_system_labels(cls, v: list[str]) -> list[str]:
        for name in v:
            if name in SYSTEM_LABELS:
                raise ValueError(
                    f"label '{name}' is a system label and cannot be added"
                )
        return v

    @field_validator("remove_label")
    @classmethod
    def _no_protected(cls, v: list[str]) -> list[str]:
        for name in v:
            if name in PROTECTED_LABELS or name in {"UNREAD"} or name.startswith("Cleanup/"):
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


def _format_errors(exc: ValidationError, raw: object) -> str:
    """Format pydantic errors, tagging rule errors with the offending rule id."""
    rules = raw.get("rules") if isinstance(raw, dict) else None
    parts = []
    for e in exc.errors():
        loc = e["loc"]
        label = ".".join(str(x) for x in loc)
        if loc and loc[0] == "rules" and len(loc) > 1 and isinstance(loc[1], int) and isinstance(rules, list):
            if 0 <= loc[1] < len(rules) and isinstance(rules[loc[1]], dict):
                rid = rules[loc[1]].get("id")
                if rid:
                    label = f"{label} (rule '{rid}')"
        parts.append(f"{label}: {e['msg']}")
    return "; ".join(parts)


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
        msgs = _format_errors(exc, raw)
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

# rules:
#   - id: old-unread-newsletters
#     match:
#       category: newsletters
#       older_than_days: 30
#       unread: true
#     actions:
#       add_label: ["Cleanup/Newsletters"]
#       archive: true
"""
