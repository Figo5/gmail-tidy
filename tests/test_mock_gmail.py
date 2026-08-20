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
    assert "Cleanup/A" in api.label_names_of("m1")
    assert "INBOX" not in api.label_names_of("m1")


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


# --- real-Gmail label semantics -----------------------------------------


def test_add_message_accepts_names_stores_ids():
    api = MockGmailApi()
    api.add_message("m1", labels={"INBOX", "Cleanup/N"})
    # stored label_ids are Gmail IDs, not names
    assert api.label_ids_of("m1") == {"INBOX", api.label_id("Cleanup/N")}
    assert api.label_names_of("m1") == {"INBOX", "Cleanup/N"}


def test_user_label_ids_are_opaque_and_distinct_from_names():
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    api.add_message("m2", labels={"Work"})
    assert api.label_id("Cleanup/N") != "Cleanup/N"
    assert api.label_id("Cleanup/N") != api.label_id("Work")
    assert api.label_name(api.label_id("Cleanup/N")) == "Cleanup/N"


def test_system_labels_map_to_themselves():
    api = MockGmailApi()
    assert api.label_id("INBOX") == "INBOX"
    assert api.label_name("INBOX") == "INBOX"


def test_labels_list_returns_distinct_ids_and_names():
    api = MockGmailApi()
    api.add_message("m1", labels={"INBOX", "Cleanup/N"})
    data = api.users().labels().list().execute()
    by_id = {lbl["id"]: lbl["name"] for lbl in data["labels"]}
    assert by_id["INBOX"] == "INBOX"
    assert by_id[api.label_id("Cleanup/N")] == "Cleanup/N"
    # distinct: no two entries share an id or a name
    ids = [lbl["id"] for lbl in data["labels"]]
    names = [lbl["name"] for lbl in data["labels"]]
    assert len(ids) == len(set(ids))
    assert len(names) == len(set(names))


def test_batch_modify_operates_on_ids():
    api = MockGmailApi()
    api.add_message("m1", labels={"INBOX"})
    cleanup_id = api.label_id("Cleanup/N")
    GmailClient(api).batch_modify(["m1"], add=[cleanup_id], remove=["INBOX"])
    assert api.label_ids_of("m1") == {cleanup_id}
    assert api.label_names_of("m1") == {"Cleanup/N"}


def test_get_returns_label_ids_not_names():
    api = MockGmailApi()
    api.add_message("m1", labels={"INBOX", "Cleanup/N"})
    data = api.users().messages().get(id="m1").execute()
    assert "Cleanup/N" not in data["labelIds"]
    assert api.label_id("Cleanup/N") in data["labelIds"]
