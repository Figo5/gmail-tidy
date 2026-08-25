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


def test_add_label_rejects_system_labels(tmp_path):
    for name in ["TRASH", "SPAM", "INBOX", "UNREAD", "STARRED"]:
        cfg = _write(tmp_path,
            "rules:\n"
            "  - id: bad\n"
            "    match: {category: promotions}\n"
            "    actions:\n"
            f"      add_label: [{name}]\n")
        with pytest.raises(ConfigError, match="bad"):
            load_config(cfg)


def test_add_label_allows_user_labels(tmp_path):
    cfg = _write(tmp_path,
        "rules:\n"
        "  - id: r1\n"
        "    match: {category: newsletters}\n"
        "    actions:\n"
        "      add_label: [Cleanup/Newsletters, Work]\n")
    cfg_obj = load_config(cfg)
    assert cfg_obj.rules[0].actions.add_label == ["Cleanup/Newsletters", "Work"]


def test_remove_label_rejects_unread(tmp_path):
    cfg = _write(tmp_path,
        "rules:\n"
        "  - id: bad\n"
        "    match: {category: promotions}\n"
        "    actions:\n"
        "      remove_label: [UNREAD]\n")
    with pytest.raises(ConfigError, match="bad"):
        load_config(cfg)


def test_remove_label_allows_inbox(tmp_path):
    cfg = _write(tmp_path,
        "rules:\n"
        "  - id: r1\n"
        "    match: {category: promotions}\n"
        "    actions:\n"
        "      remove_label: [INBOX]\n")
    cfg_obj = load_config(cfg)
    assert cfg_obj.rules[0].actions.remove_label == ["INBOX"]


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


def test_unknown_category_in_rule_rejected(tmp_path):
    cfg = _write(tmp_path,
        "rules:\n"
        "  - id: r1\n"
        "    match: {category: bogus}\n"
        "    actions: {archive: true}\n")
    with pytest.raises(ConfigError, match="category"):
        load_config(cfg)


def test_unknown_category_error_lists_expected_values(tmp_path):
    cfg = _write(tmp_path,
        "rules:\n"
        "  - id: r1\n"
        "    match: {category: bogus}\n"
        "    actions: {archive: true}\n")
    with pytest.raises(ConfigError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    assert "bogus" in msg
    assert "newsletters" in msg
    assert "large_messages" in msg


def test_unknown_category_in_protect_exclude_rejected(tmp_path):
    cfg = _write(tmp_path,
        "protect:\n"
        "  exclude:\n"
        "    - category: bogus\n")
    with pytest.raises(ConfigError, match="category"):
        load_config(cfg)


def test_all_six_categories_accepted(tmp_path):
    for cat in ("newsletters", "promotions", "receipts",
                "notifications", "old_unread", "large_messages"):
        cfg = _write(tmp_path,
            "rules:\n"
            f"  - id: r1\n"
            f"    match: {{category: {cat}}}\n"
            f"    actions: {{archive: true}}\n")
        loaded = load_config(cfg)
        assert loaded.rules[0].match.category == cat


def test_category_optional_and_null_accepted(tmp_path):
    cfg = _write(tmp_path,
        "rules:\n"
        "  - id: r1\n"
        "    match: {}\n"
        "    actions: {archive: true}\n"
        "  - id: r2\n"
        "    match: {category: null}\n"
        "    actions: {archive: true}\n")
    loaded = load_config(cfg)
    assert loaded.rules[0].match.category is None
    assert loaded.rules[1].match.category is None


# --- Task 39: malformed `rules` shapes must raise a clean ConfigError ---------
# `_format_errors` tags rule errors by reading loc[1] when the rule list is
# shaped as expected. For a non-list `rules` value the pydantic error loc is
# just ("rules",) — indexing loc[1] used to raise IndexError, leaking an
# unhandled crash out of load_config. These tests pin the clean path.


def test_rules_scalar_raises_clean_config_error(tmp_path):
    cfg = _write(tmp_path, "rules: notalist\n")
    with pytest.raises(ConfigError) as exc:
        load_config(cfg)
    assert "rules" in str(exc.value)
    assert "Traceback" not in str(exc.value)


def test_rules_null_raises_clean_config_error(tmp_path):
    cfg = _write(tmp_path, "rules: null\n")
    with pytest.raises(ConfigError) as exc:
        load_config(cfg)
    assert "rules" in str(exc.value)


def test_rules_dict_raises_clean_config_error(tmp_path):
    cfg = _write(tmp_path, "rules: {id: x}\n")
    with pytest.raises(ConfigError) as exc:
        load_config(cfg)
    assert "rules" in str(exc.value)


def test_rules_non_dict_entry_raises_clean_config_error(tmp_path):
    cfg = _write(tmp_path, "rules: [hello]\n")
    with pytest.raises(ConfigError) as exc:
        load_config(cfg)
    assert "Traceback" not in str(exc.value)


def test_rules_list_entry_error_tagged_with_rule_id(tmp_path):
    """Errors nested inside a rules list entry keep the (rule '<id>') tag."""
    cfg = _write(tmp_path,
        "rules:\n"
        "  - id: r1\n"
        "    match: notamatch\n"
        "    actions: {archive: true}\n")
    with pytest.raises(ConfigError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    assert "r1" in msg
    assert "rules.0.match" in msg
