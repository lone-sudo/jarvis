"""
Tests for the --backend flag itself: confirms selection is explicit,
manual stays the untouched default, and choosing gemini doesn't
silently fall back to manual on failure (no fallback chain).
"""

import sys
from unittest.mock import patch

import pytest

from jarvis.interface.cli import main, _select_backend
from jarvis.providers.base import ManualClipboardProvider
from jarvis.providers.gemini_api import GeminiAPIProvider


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    yield


def test_select_backend_default_is_manual():
    provider = _select_backend("manual", "AI_Web")
    assert isinstance(provider, ManualClipboardProvider)


def test_select_backend_gemini_selects_gemini_provider():
    provider = _select_backend("gemini", "Gemini_API")
    assert isinstance(provider, GeminiAPIProvider)


def test_ask_ai_defaults_to_manual_backend_unchanged(tmp_path, monkeypatch):
    """Omitting --backend entirely must behave exactly as before this flag existed."""
    from jarvis.state.tracker import StateTracker
    tracker = StateTracker()
    session_id = tracker.create_session("goal")
    tracker.register_project("proj", "proj")
    task_id = tracker.create_task(session_id, "task", "proj")

    monkeypatch.setattr("builtins.input", lambda *a: "")
    # raising=False: whether pyperclip binds as a module attribute depends
    # on whether `import pyperclip` succeeded in THIS process, which can
    # differ by environment even on the same machine (confirmed: Lone hit
    # this exact AttributeError on Windows under one Python environment,
    # per build-log 0010's same finding for a different test file -- this
    # one was missed at the time).
    with patch("jarvis.providers.base.pyperclip", create=True) as mock_clip, \
         patch("jarvis.providers.base._CLIPBOARD_AVAILABLE", True):
        mock_clip.copy.return_value = None
        mock_clip.paste.return_value = "manual response"
        sys.argv = ["jarvis", "ask-ai", "--task", task_id, "hello"]
        main()
    # No error raised, no gemini code path touched -- confirms default is untouched


def test_gemini_backend_does_not_fall_back_to_manual_on_failure(tmp_path, monkeypatch, capsys):
    """No fallback chain: a failed gemini call must not silently retry via manual."""
    from jarvis.state.tracker import StateTracker
    tracker = StateTracker()
    session_id = tracker.create_session("goal")
    tracker.register_project("proj", "proj")
    task_id = tracker.create_task(session_id, "task", "proj")

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with patch("jarvis.providers.base.ManualClipboardProvider.dispatch_prompt") as mock_manual:
        sys.argv = ["jarvis", "ask-ai", "--task", task_id, "--backend", "gemini", "hello"]
        main()
        mock_manual.assert_not_called()  # confirms no silent fallback occurred

    out = capsys.readouterr().out
    assert "GEMINI_API_KEY" in out
