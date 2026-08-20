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
