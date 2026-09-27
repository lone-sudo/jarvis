import pytest

from jarvis.tools.text_extraction import extract_readable_text
from jarvis.providers.base import dispatch_and_track, ManualClipboardProvider, Provider
from jarvis.state.tracker import StateTracker


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    yield


# --- Stdlib HTML extraction ---

def test_script_and_style_content_excluded():
    html = "<html><head><style>.x{}</style><script>track();</script></head><body><p>Real text</p></body></html>"
    result = extract_readable_text(html)
    assert "track()" not in result
    assert ".x{}" not in result
    assert "Real text" in result


def test_nav_header_footer_aside_excluded():
    html = """
    <header><nav><a href="/">Home</a></nav></header>
    <main><p>The actual article content.</p></main>
    <aside><p>Sidebar ad content</p></aside>
    <footer><p>Copyright 2026</p></footer>
    """
    result = extract_readable_text(html)
    assert "Home" not in result
    assert "Sidebar ad" not in result
    assert "Copyright" not in result
    assert "actual article content" in result


def test_malformed_html_does_not_raise():
    broken = "<html><body><p>Unclosed paragraph <div>nested weird</p></div>"
    result = extract_readable_text(broken)  # must not raise
    assert "Unclosed paragraph" in result


def test_empty_html_returns_empty_string():
    assert extract_readable_text("") == ""


def test_no_boilerplate_html_preserves_all_text():
    html = "<html><body><p>First paragraph.</p><p>Second paragraph.</p></body></html>"
    result = extract_readable_text(html)
    assert "First paragraph." in result
    assert "Second paragraph." in result


# --- Provider Protocol generalization ---

class _FakeProvider:
    """
    A minimal fake satisfying the Provider protocol structurally --
    proves dispatch_and_track works with ANY conforming object, not
    just ManualClipboardProvider. No inheritance from Provider needed;
    that's the point of a structural Protocol.
    """
    def __init__(self, canned_response="fake response"):
        self.canned_response = canned_response
        self.received_prompt = None

    def dispatch_prompt(self, prompt_text: str) -> str:
        self.received_prompt = prompt_text
        return self.canned_response


def test_manual_clipboard_provider_satisfies_protocol_structurally():
    provider = ManualClipboardProvider("Test")
    assert isinstance(provider, Provider)  # structural check via typing.Protocol


def test_fake_provider_satisfies_protocol_structurally():
    provider = _FakeProvider()
    assert isinstance(provider, Provider)


def test_standalone_dispatch_and_track_works_with_fake_provider(tmp_path):
    """
    The actual point of this round's generalization: dispatch_and_track
    is no longer hardcoded to ManualClipboardProvider.
    """
    tracker = StateTracker(db_path=tmp_path / "test.db")
    session_id = tracker.create_session("goal")
    task_id = tracker.create_task(session_id, "task", "demo-project")
    tracker.update_task_status(task_id, "IN_PROGRESS")

    fake = _FakeProvider("a real-looking answer")
    response = dispatch_and_track(fake, tracker, task_id, "do the thing", mark_done=False)

    assert response == "a real-looking answer"
    assert fake.received_prompt == "do the thing"
    final = tracker.get_active_tasks()
    assert any(t["task_id"] == task_id and t["status"] == "IN_PROGRESS" for t in final)


def test_manual_clipboard_provider_dispatch_and_track_method_still_works(tmp_path, monkeypatch):
    """
    Backward compatibility: the existing method-style call
    (provider.dispatch_and_track(...)) used by cmd_ask_ai must keep
    working unchanged.
    """
    tracker = StateTracker(db_path=tmp_path / "test.db")
    session_id = tracker.create_session("goal")
    task_id = tracker.create_task(session_id, "task", "demo-project")

    provider = ManualClipboardProvider("Test")
    monkeypatch.setattr(provider, "dispatch_prompt", lambda prompt: "mocked response")

    response = provider.dispatch_and_track(tracker, task_id, "prompt", mark_done=True)
    assert response == "mocked response"

    import sqlite3
    conn = sqlite3.connect(tracker.db_path)
    row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    assert row[0] == "DONE"
