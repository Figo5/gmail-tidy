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
