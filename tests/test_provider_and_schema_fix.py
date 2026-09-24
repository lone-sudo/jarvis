"""
Regression tests for the multi-line clipboard bug found in real use
(Lone's transcript: a multi-line AI response pasted directly at the old
ambiguous input() prompt got truncated to its first line, and the
remaining lines leaked onto the interactive shell once the script
exited) and the missing schema-validation gap it exposed alongside it.

Note on `raising=False` below: whether `pyperclip` binds as a module
attribute in providers/base.py depends on whether `import pyperclip`
succeeded in THIS process -- which varies by environment even on the
same machine (confirmed: it can differ between an interactive terminal
session and a pytest run on the same Windows machine). These tests
patch it either way, since they're deliberately substituting a mock
regardless of what the real import did.
"""

import sys
from unittest.mock import patch, MagicMock

import pytest

import jarvis.providers.base as provider_base
from jarvis.interface.cli import main
from jarvis.state.tracker import StateTracker
from jarvis.state.inbox import InboxStore


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    yield


def test_clipboard_path_calls_input_exactly_once(monkeypatch):
    """
    The core regression: the confirmation prompt must never be a place
    where multi-line content gets typed/pasted, and input() must be
    called exactly once regardless of how many lines the clipboard
    response contains.
    """
    monkeypatch.setattr(provider_base, "_CLIPBOARD_AVAILABLE", True)
    mock_pyperclip = MagicMock()
    mock_pyperclip.paste.return_value = '{"summary": "line one\\nline two\\nline three", "tags": ["a"]}'
    monkeypatch.setattr(provider_base, "pyperclip", mock_pyperclip, raising=False)

    call_count = {"n": 0}
    def counting_input(prompt=""):
        call_count["n"] += 1
        return ""
    monkeypatch.setattr("builtins.input", counting_input)

    provider = provider_base.ManualClipboardProvider("Test")
    result = provider.dispatch_prompt("prompt text")

    assert call_count["n"] == 1
    assert "line one" in result and "line three" in result  # full multi-line content preserved


def test_empty_clipboard_falls_back_to_manual_multiline_entry(monkeypatch):
    monkeypatch.setattr(provider_base, "_CLIPBOARD_AVAILABLE", True)
    mock_pyperclip = MagicMock()
    mock_pyperclip.paste.return_value = ""  # empty/stale clipboard
    monkeypatch.setattr(provider_base, "pyperclip", mock_pyperclip, raising=False)

    inputs = iter(["", "line one", "line two", "END"])  # first "" answers the confirm prompt
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))

    provider = provider_base.ManualClipboardProvider("Test")
    result = provider.dispatch_prompt("prompt text")
    assert result == "line one\nline two"


def test_manual_fallback_reads_until_terminator_not_truncated(monkeypatch):
    monkeypatch.setattr(provider_base, "_CLIPBOARD_AVAILABLE", False)
    inputs = iter(['{"summary": "a",', '"tags": ["x", "y"],', '"actionable": true}', "END"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))

    provider = provider_base.ManualClipboardProvider("Test")
    result = provider.dispatch_prompt("prompt text")
    assert result == '{"summary": "a",\n"tags": ["x", "y"],\n"actionable": true}'


# --- Classification schema validation ---

def _save_one_item(title="test item"):
    tracker = StateTracker()
    store = InboxStore(tracker.db_path)
    sys.argv = ["jarvis", "save", "--text", "some content", "--title", title]
    main()
    return store.list_items()[0]["item_id"]


def test_completely_wrong_schema_rejected_stays_unprocessed(tmp_path, capsys):
    item_id = _save_one_item()
    totally_wrong = '{"answer": "42", "explanation": "not our format at all"}'

    with patch("jarvis.interface.cli.ManualClipboardProvider.dispatch_prompt", return_value=totally_wrong):
        sys.argv = ["jarvis", "inbox-process"]
        main()

    out = capsys.readouterr().out
    assert "doesn't match the expected classification format" in out

    tracker = StateTracker()
    store = InboxStore(tracker.db_path)
    assert store.get_item(item_id)["status"] == "UNPROCESSED"


def test_partial_schema_match_warns_but_proceeds(tmp_path, capsys):
    """
    Reproduces item 1 from Lone's actual transcript: valid JSON, wrong
    shape overall, but with a real usable summary present. Should warn
    loudly rather than silently default everything.
    """
    item_id = _save_one_item()
    partial = (
        '{"title":"test fetch","source_url":"https://example.com","project_key":null,'
        '"category":"Database","summary":"A real summary that IS present.","confidence":0.99}'
    )

    with patch("jarvis.interface.cli.ManualClipboardProvider.dispatch_prompt", return_value=partial):
        sys.argv = ["jarvis", "inbox-process"]
        main()

    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "missing expected field" in out

    tracker = StateTracker()
    store = InboxStore(tracker.db_path)
    item = store.get_item(item_id)
    assert item["status"] == "PROCESSED"  # proceeded, since summary+confidence were present
    assert item["summary"] == "A real summary that IS present."
    assert item["tags"] == "[]"  # correctly empty, not guessed


def test_full_schema_match_no_warning(tmp_path, capsys):
    item_id = _save_one_item()
    correct = '{"summary": "s", "tags": ["a", "b"], "actionable": false, "related_project": null, "confidence": 0.8}'

    with patch("jarvis.interface.cli.ManualClipboardProvider.dispatch_prompt", return_value=correct):
        sys.argv = ["jarvis", "inbox-process"]
        main()

    out = capsys.readouterr().out
    assert "WARNING" not in out
    assert "ERROR" not in out


def test_non_dict_json_rejected(tmp_path, capsys):
    item_id = _save_one_item()
    with patch("jarvis.interface.cli.ManualClipboardProvider.dispatch_prompt", return_value='["just", "a", "list"]'):
        sys.argv = ["jarvis", "inbox-process"]
        main()

    out = capsys.readouterr().out
    assert "not a JSON object" in out
    tracker = StateTracker()
    store = InboxStore(tracker.db_path)
    assert store.get_item(item_id)["status"] == "UNPROCESSED"
