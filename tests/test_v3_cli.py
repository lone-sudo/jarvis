"""
CLI-level tests for V3: the allowlist commands and the --fetch flow's
guardrails. NetworkInspector's own mechanics are tested directly in
test_network.py against a real local server; these tests confirm the
CLI wiring around it (confirmation, decline behavior, no-item-on-
rejection, plain --url never fetching) without needing real external
network access.
"""

import sys
from unittest.mock import patch

import pytest

from jarvis.interface.cli import main
from jarvis.state.tracker import StateTracker
from jarvis.state.inbox import InboxStore
from jarvis.tools.network import FetchResult, NetworkFetchError


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    yield


def _run(argv, input_value=None, monkeypatch=None):
    if monkeypatch is not None and input_value is not None:
        monkeypatch.setattr("builtins.input", lambda prompt="": input_value)
    old_argv = sys.argv
    sys.argv = ["jarvis"] + argv
    try:
        main()
    finally:
        sys.argv = old_argv


def _inbox_count(tmp_path):
    tracker = StateTracker()
    store = InboxStore(tracker.db_path)
    return len(store.list_items())


def test_plain_url_save_never_fetches(tmp_path):
    """The core default: no --fetch flag means zero network activity, ever."""
    with patch("jarvis.interface.cli.network_tool.fetch") as mock_fetch:
        _run(["save", "--url", "https://example.com/x", "--title", "t"])
        mock_fetch.assert_not_called()
    assert _inbox_count(tmp_path) == 1


def test_fetch_on_unallowlisted_domain_rejected_no_item_created(tmp_path, capsys):
    _run(["save", "--url", "https://notallowed.com/x", "--title", "t", "--fetch"])
    out = capsys.readouterr().out
    assert "not on the network allowlist" in out
    assert _inbox_count(tmp_path) == 0


def test_declined_confirmation_no_fetch_no_item(tmp_path, monkeypatch):
    _run(["network-allow", "example.com"])
    with patch("jarvis.interface.cli.network_tool.fetch") as mock_fetch:
        _run(["save", "--url", "https://example.com/x", "--title", "t", "--fetch"], input_value="n", monkeypatch=monkeypatch)
        mock_fetch.assert_not_called()
    assert _inbox_count(tmp_path) == 0


def test_confirmed_fetch_creates_unprocessed_item_with_fetched_content(tmp_path, monkeypatch):
    _run(["network-allow", "example.com"])
    fake_result = FetchResult(
        original_url="https://example.com/x", final_url="https://example.com/x",
        resolved_ip="93.184.216.34", status_code=200, content_type="text/html",
        bytes_received=100, redirect_count=0, body_text="fetched body content",
    )
    with patch("jarvis.interface.cli.network_tool.fetch", return_value=fake_result):
        _run(["save", "--url", "https://example.com/x", "--title", "t", "--fetch"], input_value="y", monkeypatch=monkeypatch)

    tracker = StateTracker()
    store = InboxStore(tracker.db_path)
    items = store.list_items()
    assert len(items) == 1
    assert items[0]["status"] == "UNPROCESSED"  # no auto-classification, no auto-task


def test_fetch_failure_creates_no_item(tmp_path, monkeypatch):
    _run(["network-allow", "example.com"])
    with patch("jarvis.interface.cli.network_tool.fetch", side_effect=NetworkFetchError("simulated failure")):
        _run(["save", "--url", "https://example.com/x", "--title", "t", "--fetch"], input_value="y", monkeypatch=monkeypatch)
    assert _inbox_count(tmp_path) == 0


def test_network_allow_and_list_roundtrip(tmp_path, capsys):
    _run(["network-list"])
    assert "empty" in capsys.readouterr().out.lower()

    _run(["network-allow", "Example.COM."])  # deliberately messy casing/trailing dot
    _run(["network-list"])
    out = capsys.readouterr().out
    assert "example.com" in out  # normalized on the way in
