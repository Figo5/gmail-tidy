# tests/test_checkpoint.py
import json

import pytest

from gmail_tidy.config import Config, Rule, MatchConfig, Actions
from gmail_tidy.checkpoint import (
    RuleCheckpoint,
    ScanCheckpoint,
    config_fingerprint,
    load_checkpoint,
    save_checkpoint,
)


def _config(**kw):
    defaults = dict(
        rules=[
            Rule(id="r1", match=MatchConfig(subject_contains=["newsletter"]),
                 actions=Actions(add_label=["Cleanup/N"], archive=True)),
        ]
    )
    defaults.update(kw)
    return Config(**defaults)


def test_config_fingerprint_is_stable():
    assert config_fingerprint(_config()) == config_fingerprint(_config())


def test_config_fingerprint_changes_with_rules(tmp_path):
    a = config_fingerprint(_config())
    b = config_fingerprint(_config(rules=[]))
    assert a != b


def test_config_fingerprint_changes_with_include():
    a = config_fingerprint(_config())
    b = config_fingerprint(_config(include=["label:work"]))
    assert a != b


def test_load_missing_file_returns_fresh(tmp_path):
    cfg = _config()
    cp = load_checkpoint(tmp_path / "nonexistent.json", cfg)
    assert cp.config_fingerprint == config_fingerprint(cfg)
    assert cp.rules == {}


def test_load_corrupt_file_returns_fresh(tmp_path):
    cfg = _config()
    path = tmp_path / "checkpoint.json"
    path.write_text("{ not json", encoding="utf-8")
    cp = load_checkpoint(path, cfg)
    assert cp.config_fingerprint == config_fingerprint(cfg)
    assert cp.rules == {}


def test_load_fingerprint_mismatch_returns_fresh(tmp_path):
    cfg = _config()
    path = tmp_path / "checkpoint.json"
    stale = ScanCheckpoint(config_fingerprint="old-fingerprint",
                           rules={"r1": RuleCheckpoint(page_token="tok-1")})
    save_checkpoint(path, stale)
    cp = load_checkpoint(path, cfg)
    assert cp.config_fingerprint == config_fingerprint(cfg)
    assert cp.rules == {}


def test_save_load_roundtrip(tmp_path):
    cfg = _config()
    path = tmp_path / "checkpoint.json"
    cp = ScanCheckpoint(
        config_fingerprint=config_fingerprint(cfg),
        rules={"r1": RuleCheckpoint(page_token="tok-1"), "r2": RuleCheckpoint(page_token=None)},
    )
    save_checkpoint(path, cp)
    loaded = load_checkpoint(path, cfg)
    assert loaded.config_fingerprint == cp.config_fingerprint
    assert loaded.rules["r1"].page_token == "tok-1"
    assert loaded.rules["r2"].page_token is None


def test_save_writes_valid_json(tmp_path):
    cfg = _config()
    path = tmp_path / "checkpoint.json"
    save_checkpoint(path, ScanCheckpoint(config_fingerprint=config_fingerprint(cfg)))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["config_fingerprint"] == config_fingerprint(cfg)
    assert data["rules"] == {}


# --- exhausted field -------------------------------------------------------


def test_rule_checkpoint_exhausted_defaults_false():
    assert RuleCheckpoint().exhausted is False
    assert RuleCheckpoint(page_token="tok-1").exhausted is False


def test_save_load_roundtrip_preserves_exhausted(tmp_path):
    cfg = _config()
    path = tmp_path / "checkpoint.json"
    cp = ScanCheckpoint(
        config_fingerprint=config_fingerprint(cfg),
        rules={
            "r1": RuleCheckpoint(page_token=None, exhausted=True),
            "r2": RuleCheckpoint(page_token="tok-2", exhausted=False),
        },
    )
    save_checkpoint(path, cp)
    loaded = load_checkpoint(path, cfg)
    assert loaded.rules["r1"].page_token is None
    assert loaded.rules["r1"].exhausted is True
    assert loaded.rules["r2"].page_token == "tok-2"
    assert loaded.rules["r2"].exhausted is False
    # the raw JSON persists the exhausted field alongside page_token
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["rules"]["r1"]["exhausted"] is True
    assert data["rules"]["r2"]["exhausted"] is False


def test_load_old_checkpoint_without_exhausted_defaults_false(tmp_path):
    """A checkpoint.json written before the exhausted field existed must load
    as exhausted=False for every rule (safe backward-compat: an 'exhausted' rule
    misreads as not-exhausted, causing a harmless re-scan, never a skip)."""
    cfg = _config()
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps({
        "config_fingerprint": config_fingerprint(cfg),
        "rules": {"r1": {"page_token": "tok-1"}},
    }), encoding="utf-8")
    loaded = load_checkpoint(path, cfg)
    assert loaded.rules["r1"].page_token == "tok-1"
    assert loaded.rules["r1"].exhausted is False
