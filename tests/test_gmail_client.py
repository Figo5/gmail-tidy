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
    assert "Cleanup/A" in api.label_names_of("m1")
    assert "INBOX" not in api.label_names_of("m2")


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


# --- label boundary conversion -------------------------------------------


def test_fetch_label_index_maps_names_to_ids():
    api = MockGmailApi()
    api.add_message("m1", labels={"INBOX", "Cleanup/N"})
    idx = GmailClient(api).fetch_label_index()
    assert idx.name_to_id("Cleanup/N") == api.label_id("Cleanup/N")
    assert idx.id_to_name(api.label_id("Cleanup/N")) == "Cleanup/N"
    assert idx.name_to_id("INBOX") == "INBOX"


def test_get_meta_returns_name_labels():
    api = MockGmailApi()
    api.add_message("m1", labels={"INBOX", "Cleanup/N"})
    idx = GmailClient(api).fetch_label_index()
    meta = GmailClient(api).get_meta("m1", idx)
    assert meta.labels == {"INBOX", "Cleanup/N"}
    assert "INBOX" in meta.labels
    assert api.label_id("Cleanup/N") not in meta.labels


def test_get_meta_keeps_unknown_ids_raw():
    """An ID not in the index (e.g. a label created after the index was
    fetched) is kept as-is so undo's exact-set safety never drops state."""
    api = MockGmailApi()
    api.add_message("m1", labels={"INBOX"})
    idx = GmailClient(api).fetch_label_index()
    # a label appears on the message after the index was fetched
    api.store["m1"].label_ids.add("Label_999")
    meta = GmailClient(api).get_meta("m1", idx)
    assert "Label_999" in meta.labels


def test_ensure_label_resolves_existing():
    api = MockGmailApi()
    api.add_message("m1", labels={"Cleanup/N"})
    idx = GmailClient(api).fetch_label_index()
    assert GmailClient(api).ensure_label("Cleanup/N", idx) == api.label_id("Cleanup/N")
    assert api.label_id("Cleanup/N") in api._user_labels.values()


def test_ensure_label_creates_missing():
    api = MockGmailApi()
    idx = GmailClient(api).fetch_label_index()
    label_id = GmailClient(api).ensure_label("Cleanup/New", idx)
    assert label_id == api.label_id("Cleanup/New")
    assert idx.name_to_id("Cleanup/New") == label_id
