from pathlib import Path

import pytest

from jarvis.state.tracker import StateTracker
from jarvis.tools.fs import FileSystemInspector
from jarvis.tools.git import GitInspector
from jarvis.tools.base import run_logged
from jarvis.providers.base import ManualClipboardProvider


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    yield


@pytest.fixture
def tracker(tmp_path):
    return StateTracker(db_path=tmp_path / "test.db")


def test_write_file_confirmed_writes_content():
    result = FileSystemInspector.write_file("notes.txt", "hello world", auto_confirm=True)
    assert "Wrote" in result
    from jarvis.policy.rules import SecurityPolicy
    written = (SecurityPolicy.get_workspace_root() / "notes.txt").read_text()
    assert written == "hello world"


def test_write_file_declined_does_not_write():
    result = FileSystemInspector.write_file("notes.txt", "hello world", auto_confirm=False)
    assert "denied by policy" in result.lower()
    from jarvis.policy.rules import SecurityPolicy
    assert not (SecurityPolicy.get_workspace_root() / "notes.txt").exists()


def test_declined_write_is_logged_as_rejected_by_policy(tracker):
    session_id = tracker.create_session("goal")
    task_id = tracker.create_task(session_id, "task", "demo-project")

    run_logged(
        tracker, task_id, "write_file", "SAFE_WRITE",
        lambda: FileSystemInspector.write_file("notes.txt", "content", auto_confirm=False),
    )

    import sqlite3
    conn = sqlite3.connect(tracker.db_path)
    row = conn.execute(
        "SELECT status FROM executions WHERE task_id = ? ORDER BY executed_at DESC LIMIT 1", (task_id,)
    ).fetchone()
    assert row[0] == "REJECTED_BY_POLICY"


def test_confirmed_write_is_logged_as_success(tracker):
    session_id = tracker.create_session("goal")
    task_id = tracker.create_task(session_id, "task", "demo-project")

    run_logged(
        tracker, task_id, "write_file", "SAFE_WRITE",
        lambda: FileSystemInspector.write_file("notes.txt", "content", auto_confirm=True),
    )

    import sqlite3
    conn = sqlite3.connect(tracker.db_path)
    row = conn.execute(
        "SELECT status FROM executions WHERE task_id = ? ORDER BY executed_at DESC LIMIT 1", (task_id,)
    ).fetchone()
    assert row[0] == "SUCCESS"


def test_git_commit_declined_leaves_repo_dirty(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(workspace))
    import subprocess
    repo = workspace / "proj"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    (repo / "a.txt").write_text("y")

    result = GitInspector.commit_changes("proj", "should not land", auto_confirm=False)
    assert "denied by policy" in result.lower()
    status = subprocess.run(["git", "status", "--short"], cwd=repo, capture_output=True, text=True).stdout
    assert "a.txt" in status  # still uncommitted


def test_provider_dispatch_transitions_task_through_awaiting_user(tracker, monkeypatch):
    session_id = tracker.create_session("goal")
    task_id = tracker.create_task(session_id, "task", "demo-project")
    tracker.update_task_status(task_id, "IN_PROGRESS")

    provider = ManualClipboardProvider("TestProvider")
    # Simulate the human pasting a response back without a real terminal.
    monkeypatch.setattr(provider, "dispatch_prompt", lambda prompt: "simulated AI response")

    seen_status_during_dispatch = {}

    def fake_dispatch(prompt):
        # At this point dispatch_and_track has already set AWAITING_USER.
        row = [t for t in tracker.get_active_tasks() if t["task_id"] == task_id][0]
        seen_status_during_dispatch["status"] = row["status"]
        return "simulated AI response"

    monkeypatch.setattr(provider, "dispatch_prompt", fake_dispatch)

    response = provider.dispatch_and_track(tracker, task_id, "do the thing", mark_done=False)

    assert response == "simulated AI response"
    assert seen_status_during_dispatch["status"] == "AWAITING_USER"
    final = [t for t in tracker.get_active_tasks() if t["task_id"] == task_id][0]
    assert final["status"] == "IN_PROGRESS"


def test_provider_dispatch_mark_done(tracker, monkeypatch):
    session_id = tracker.create_session("goal")
    task_id = tracker.create_task(session_id, "task", "demo-project")

    provider = ManualClipboardProvider("TestProvider")
    monkeypatch.setattr(provider, "dispatch_prompt", lambda prompt: "final answer")

    provider.dispatch_and_track(tracker, task_id, "wrap it up", mark_done=True)

    # DONE tasks are no longer "active", so query directly.
    import sqlite3
    conn = sqlite3.connect(tracker.db_path)
    row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    assert row[0] == "DONE"


def test_interrupted_dispatch_leaves_task_at_awaiting_user(tracker, monkeypatch):
    """
    If the process dies while waiting on the human (e.g. terminal closed
    before pasting a response back), the task must be left at
    AWAITING_USER, not silently reset — that's exactly the state
    `jarvis resume` should surface.
    """
    session_id = tracker.create_session("goal")
    task_id = tracker.create_task(session_id, "task", "demo-project")

    provider = ManualClipboardProvider("TestProvider")

    def crashes(prompt):
        raise KeyboardInterrupt("simulated terminal close")

    monkeypatch.setattr(provider, "dispatch_prompt", crashes)

    with pytest.raises(KeyboardInterrupt):
        provider.dispatch_and_track(tracker, task_id, "do the thing", mark_done=False)

    import sqlite3
    conn = sqlite3.connect(tracker.db_path)
    row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    assert row[0] == "AWAITING_USER"
