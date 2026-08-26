# tests/test_checkpoint.py
import json

import pytest

from gmail_tidy.config import Config, Rule, MatchConfig, Actions
from gmail_tidy.checkpoint import (
    RuleCheckpoint,
    ScanCheckpoint,
    config_fingerprint,
    load_checkpoint,
    merge_checkpoint,
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


def test_config_fingerprint_of_filtered_subset_differs_from_full():
    """Invariant this fix relies on: fingerprinting a --rules-filtered config
    yields a DIFFERENT hash than fingerprinting the full config. That is why the
    scan/run commands must pass the FULL config into load_checkpoint /
    config_fingerprint — loading with the filtered config would mismatch the
    stored fingerprint and silently reset every rule's checkpoint state."""
    full = Config(
        rules=[
            Rule(id="r1", match=MatchConfig(subject_contains=["alpha"]),
                 actions=Actions(add_label=["Cleanup/A"], archive=True)),
            Rule(id="r2", match=MatchConfig(subject_contains=["beta"]),
                 actions=Actions(add_label=["Cleanup/B"], archive=True)),
        ]
    )
    filtered = Config(rules=[full.rules[0]])  # the --rules r1 subset
    assert config_fingerprint(filtered) != config_fingerprint(full)


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


# --- merge_checkpoint (--rules scoped scan state preservation) -------------


def test_merge_checkpoint_carries_prior_unselected_entries_forward():
    """A scoped scan (fresh) covers only r1; r2's prior entry is carried
    forward unchanged by the merge, keeping the saved checkpoint complete."""
    prior = ScanCheckpoint(
        config_fingerprint="full-fp",
        rules={"r1": RuleCheckpoint(page_token="tok1-old", exhausted=False),
               "r2": RuleCheckpoint(page_token="tok2", exhausted=True)},
    )
    fresh = ScanCheckpoint(
        config_fingerprint="full-fp",
        rules={"r1": RuleCheckpoint(page_token=None, exhausted=True)},
    )
    merged = merge_checkpoint(prior, fresh)
    # selected rule: fresh wins
    assert merged.rules["r1"].page_token is None
    assert merged.rules["r1"].exhausted is True
    # unselected rule: prior entry preserved byte-for-byte
    assert merged.rules["r2"].page_token == "tok2"
    assert merged.rules["r2"].exhausted is True
    # fingerprint stays the full-config hash
    assert merged.config_fingerprint == "full-fp"


def test_merge_checkpoint_none_prior_is_identity():
    fresh = ScanCheckpoint(config_fingerprint="fp",
                           rules={"r1": RuleCheckpoint(page_token="tok1")})
    merged = merge_checkpoint(None, fresh)
    assert merged.rules == fresh.rules
    assert merged.config_fingerprint == "fp"


def test_merge_checkpoint_empty_prior_keeps_fresh_only():
    fresh = ScanCheckpoint(config_fingerprint="fp",
                           rules={"r1": RuleCheckpoint(page_token="tok1")})
    merged = merge_checkpoint(ScanCheckpoint(config_fingerprint="fp"), fresh)
    assert merged.rules == fresh.rules


# --- valid-JSON but wrong-shape data (Task 36) ------------------------------
# A checkpoint.json that parses as JSON but is not the expected shape must be
# treated exactly like a missing/corrupt/fingerprint-mismatch file: a fresh
# checkpoint, never a raw AttributeError/TypeError traceback leaking through
# scan/run/summary. Each fixture writes valid JSON with the CURRENT fingerprint
# so the mismatch is purely a shape error, not a config-change reset.


def _write_checkpoint(path, payload, cfg):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_checkpoint(path, cfg)


def test_load_top_level_list_returns_fresh(tmp_path):
    """checkpoint.json = `[1,2,3]` is valid JSON but not the expected object."""
    cfg = _config()
    cp = _write_checkpoint(tmp_path / "checkpoint.json", [1, 2, 3], cfg)
    assert cp.config_fingerprint == config_fingerprint(cfg)
    assert cp.rules == {}


def test_load_top_level_string_returns_fresh(tmp_path):
    """checkpoint.json = `"hello"` is valid JSON but not the expected object."""
    cfg = _config()
    cp = _write_checkpoint(tmp_path / "checkpoint.json", "hello", cfg)
    assert cp.config_fingerprint == config_fingerprint(cfg)
    assert cp.rules == {}


def test_load_rules_list_returns_fresh(tmp_path):
    """`rules` as a JSON list instead of an object is a shape error."""
    cfg = _config()
    payload = {"config_fingerprint": config_fingerprint(cfg),
               "rules": [{"page_token": "tok-1"}]}
    cp = _write_checkpoint(tmp_path / "checkpoint.json", payload, cfg)
    assert cp.config_fingerprint == config_fingerprint(cfg)
    assert cp.rules == {}


def test_load_rules_string_returns_fresh(tmp_path):
    """`rules` as a JSON string instead of an object is a shape error."""
    cfg = _config()
    payload = {"config_fingerprint": config_fingerprint(cfg),
               "rules": "not-an-object"}
    cp = _write_checkpoint(tmp_path / "checkpoint.json", payload, cfg)
    assert cp.config_fingerprint == config_fingerprint(cfg)
    assert cp.rules == {}


def test_load_non_dict_rule_entry_dropped(tmp_path):
    """A rule whose entry is a string/list/scalar is dropped, not crashed on."""
    cfg = _config()
    payload = {"config_fingerprint": config_fingerprint(cfg),
               "rules": {"r1": "garbage", "r2": ["tok"], "r3": 42}}
    cp = _write_checkpoint(tmp_path / "checkpoint.json", payload, cfg)
    assert cp.config_fingerprint == config_fingerprint(cfg)
    assert cp.rules == {}


def test_load_mixed_valid_and_bad_rule_entries_keeps_valid(tmp_path):
    """Well-formed rule entries survive; only the non-dict ones are dropped."""
    cfg = _config()
    payload = {"config_fingerprint": config_fingerprint(cfg),
               "rules": {
                   "r1": {"page_token": "tok-1", "exhausted": True},
                   "r2": "garbage",
                   "r3": ["not-a-dict"],
               }}
    cp = _write_checkpoint(tmp_path / "checkpoint.json", payload, cfg)
    assert cp.config_fingerprint == config_fingerprint(cfg)
    assert sorted(cp.rules) == ["r1"]
    assert cp.rules["r1"].page_token == "tok-1"
    assert cp.rules["r1"].exhausted is True


# --- invalid UTF-8 bytes (Task 40) -------------------------------------------
# A checkpoint.json containing bytes that do not decode as UTF-8 (e.g. a
# partial write from a crash, or hand-corruption) must degrade exactly like a
# missing/corrupt file: a fresh checkpoint, never a raw UnicodeDecodeError
# leaking through scan/run/summary. Valid files keep their behavior.


def test_load_invalid_utf8_bytes_returns_fresh(tmp_path):
    """Invalid UTF-8 bytes in checkpoint.json are treated like a corrupt file."""
    cfg = _config()
    path = tmp_path / "checkpoint.json"
    path.write_bytes(b"\xff\xfe\x00\x00")
    cp = load_checkpoint(path, cfg)
    assert cp.config_fingerprint == config_fingerprint(cfg)
    assert cp.rules == {}
