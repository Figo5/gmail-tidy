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
