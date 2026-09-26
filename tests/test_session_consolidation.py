import sys
from unittest.mock import patch

import pytest

from jarvis.state.session_consolidation import determine_ownership, build_candidate, build_consolidated_note
from jarvis.state.tracker import StateTracker
from jarvis.state.database import get_connection
from jarvis.memory.markdown import ProjectMemory
from jarvis.interface.cli import main


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    yield


def _task(project_key, status="DONE", title="t"):
    return {"project_key": project_key, "status": status, "title": title}


# --- Ownership derivation (the core correctness question) ---

def test_same_project_tasks_eligible():
    key, reason = determine_ownership([_task("proj-a"), _task("proj-a")])
    assert key == "proj-a"
    assert reason is None


def test_mixed_project_tasks_ineligible():
    key, reason = determine_ownership([_task("proj-a"), _task("proj-b")])
    assert key is None
    assert "multiple projects" in reason


def test_no_tasks_ineligible():
    key, reason = determine_ownership([])
    assert key is None
    assert "no tasks" in reason


def test_single_task_eligible():
    key, reason = determine_ownership([_task("proj-a")])
    assert key == "proj-a"
    assert reason is None


# --- Full CLI flow ---

def _complete_session_with_tasks(tracker, goal, task_specs):
    """task_specs: list of (title, project_key, final_status)."""
    session_id = tracker.create_session(goal)
    for title, project_key, status in task_specs:
        task_id = tracker.create_task(session_id, title, project_key)
        if status != "PENDING":
            tracker.update_task_status(task_id, "IN_PROGRESS")
            if status != "IN_PROGRESS":
                tracker.update_task_status(task_id, status)
    with get_connection(tracker.db_path) as conn:
        conn.execute("UPDATE sessions SET status='COMPLETED' WHERE session_id=?", (session_id,))
    return session_id


def test_eligible_session_consolidates_and_writes_to_project_memory(tmp_path):
    tracker = StateTracker()
    tracker.register_project("proj-a", "proj-a")
    session_id = _complete_session_with_tasks(
        tracker, "Data cleaning", [("Load data", "proj-a", "DONE"), ("Clean data", "proj-a", "DONE")]
    )

    with patch("builtins.input", return_value="y"):
        sys.argv = ["jarvis", "session-consolidate"]
        main()

    with get_connection(tracker.db_path) as conn:
        row = conn.execute("SELECT consolidated FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        assert row["consolidated"] == 1

    notes = ProjectMemory.read_latest("proj-a", max_chars=5000)
    assert "Data cleaning" in notes


def test_mixed_project_session_skipped_not_consolidated(tmp_path):
    tracker = StateTracker()
    tracker.register_project("proj-a", "proj-a")
    tracker.register_project("proj-b", "proj-b")
    session_id = _complete_session_with_tasks(
        tracker, "Mixed work", [("Task A", "proj-a", "DONE"), ("Task B", "proj-b", "DONE")]
    )

    with patch("builtins.input", return_value="y"):
        sys.argv = ["jarvis", "session-consolidate"]
        main()

    with get_connection(tracker.db_path) as conn:
        row = conn.execute("SELECT consolidated FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        assert row["consolidated"] == 0  # never touched


def test_zero_task_session_skipped(tmp_path):
    tracker = StateTracker()
    session_id = tracker.create_session("Empty")
    with get_connection(tracker.db_path) as conn:
        conn.execute("UPDATE sessions SET status='COMPLETED' WHERE session_id=?", (session_id,))

    with patch("builtins.input", return_value="y"):
        sys.argv = ["jarvis", "session-consolidate"]
        main()

    with get_connection(tracker.db_path) as conn:
        row = conn.execute("SELECT consolidated FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        assert row["consolidated"] == 0


def test_non_completed_session_never_considered(tmp_path, capsys):
    tracker = StateTracker()
    tracker.register_project("proj-a", "proj-a")
    session_id = tracker.create_session("Still going")
    tracker.create_task(session_id, "Task", "proj-a")
    # deliberately never marked COMPLETED -- stays ACTIVE

    sys.argv = ["jarvis", "session-consolidate"]
    main()
    out = capsys.readouterr().out
    assert "No eligible sessions found" in out


def test_declined_confirmation_leaves_session_unconsolidated(tmp_path):
    tracker = StateTracker()
    tracker.register_project("proj-a", "proj-a")
    session_id = _complete_session_with_tasks(tracker, "Goal", [("T", "proj-a", "DONE")])

    with patch("builtins.input", return_value="n"):
        sys.argv = ["jarvis", "session-consolidate"]
        main()

    with get_connection(tracker.db_path) as conn:
        row = conn.execute("SELECT consolidated FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        assert row["consolidated"] == 0


def test_write_failure_does_not_mark_consolidated(tmp_path):
    """The atomicity invariant, tested at the CLI level."""
    tracker = StateTracker()
    tracker.register_project("proj-a", "proj-a")
    session_id = _complete_session_with_tasks(tracker, "Goal", [("T", "proj-a", "DONE")])

    with patch("jarvis.interface.cli.ProjectMemory.append_note", return_value="ERROR: simulated failure"):
        with patch("builtins.input", return_value="y"):
            sys.argv = ["jarvis", "session-consolidate"]
            main()

    with get_connection(tracker.db_path) as conn:
        row = conn.execute("SELECT consolidated FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        assert row["consolidated"] == 0  # NOT marked despite the "yes"


def test_idempotent_rerun_does_not_reconsolidate(tmp_path):
    tracker = StateTracker()
    tracker.register_project("proj-a", "proj-a")
    session_id = _complete_session_with_tasks(tracker, "Goal", [("T", "proj-a", "DONE")])

    with patch("builtins.input", return_value="y"):
        sys.argv = ["jarvis", "session-consolidate"]
        main()

    notes_after_first = ProjectMemory.read_latest("proj-a", max_chars=5000)

    # Second run: should find nothing (already consolidated), no duplicate note.
    with patch("builtins.input", return_value="y"):
        sys.argv = ["jarvis", "session-consolidate"]
        main()

    notes_after_second = ProjectMemory.read_latest("proj-a", max_chars=5000)
    assert notes_after_first == notes_after_second  # unchanged -- no duplicate write


def test_aborted_task_shown_correctly_in_note():
    from jarvis.state.session_consolidation import build_candidate
    session = {"session_id": "SES-X", "primary_goal": "Goal"}
    tasks = [_task("proj-a", status="DONE", title="Done task"), _task("proj-a", status="ABORTED", title="Aborted task")]
    candidate = build_candidate(session, tasks)
    note = build_consolidated_note(candidate)
    assert "Done task" in note and "Aborted task" in note
    assert "1 completed, 1 aborted, 2 total" in note
