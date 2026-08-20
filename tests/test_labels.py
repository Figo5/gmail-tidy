# tests/test_labels.py
"""LabelIndex: the single place that maps between canonical label NAMES and
Gmail label IDs. System labels (INBOX, UNREAD, ...) have id == name in Gmail
and map to themselves; user labels get opaque IDs (Label_123...)."""

from gmail_tidy.labels import LabelIndex, SYSTEM_LABELS


def test_system_labels_map_to_themselves():
    idx = LabelIndex()
    for name in ("INBOX", "UNREAD", "STARRED", "IMPORTANT", "SPAM", "TRASH",
                 "DRAFT", "SENT", "CHAT"):
        assert idx.name_to_id(name) == name
        assert idx.id_to_name(name) == name
        assert idx.resolve_name(name) == name
        assert idx.resolve_id(name) == name


def test_system_labels_are_always_known():
    assert "INBOX" in SYSTEM_LABELS
    assert "UNREAD" in SYSTEM_LABELS


def test_from_labels_builds_bidirectional_mapping():
    idx = LabelIndex.from_labels([
        {"id": "Label_1", "name": "Cleanup/N"},
        {"id": "Label_2", "name": "Work"},
    ])
    assert idx.name_to_id("Cleanup/N") == "Label_1"
    assert idx.id_to_name("Label_1") == "Cleanup/N"
    assert idx.name_to_id("Work") == "Label_2"
    assert idx.id_to_name("Label_2") == "Work"


def test_unknown_names_and_ids_resolve_to_none():
    idx = LabelIndex.from_labels([{"id": "Label_1", "name": "Cleanup/N"}])
    assert idx.name_to_id("DoesNotExist") is None
    assert idx.id_to_name("Label_999") is None
    assert idx.resolve_name("DoesNotExist") is None
    assert idx.resolve_id("Label_999") is None


def test_add_registers_new_label():
    idx = LabelIndex()
    idx.add("Cleanup/N", "Label_7")
    assert idx.name_to_id("Cleanup/N") == "Label_7"
    assert idx.id_to_name("Label_7") == "Cleanup/N"


def test_add_is_idempotent_for_same_name():
    idx = LabelIndex()
    idx.add("Cleanup/N", "Label_7")
    idx.add("Cleanup/N", "Label_7")
    assert idx.name_to_id("Cleanup/N") == "Label_7"


def test_names_and_ids_include_system_labels():
    idx = LabelIndex.from_labels([{"id": "Label_1", "name": "Cleanup/N"}])
    assert "INBOX" in idx.names()
    assert "INBOX" in idx.ids()
    assert "Cleanup/N" in idx.names()
    assert "Label_1" in idx.ids()
