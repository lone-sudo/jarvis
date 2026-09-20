import os
import tempfile
from pathlib import Path

import pytest

from jarvis.state.tracker import StateTracker


@pytest.fixture
def tracker(tmp_path):
    db_path = tmp_path / "test_jarvis.db"
    return StateTracker(db_path=db_path)


def test_create_session(tracker):
    session_id = tracker.create_session("Test goal")
    assert session_id.startswith("SES-")


def test_create_task_and_status_transition(tracker):
    session_id = tracker.create_session("Test goal")
    task_id = tracker.create_task(session_id, "Do a thing", "demo-project")
    assert task_id.startswith("TSK-")

    tracker.update_task_status(task_id, "IN_PROGRESS")
    active = tracker.get_active_tasks()
    assert any(t["task_id"] == task_id and t["status"] == "IN_PROGRESS" for t in active)


def test_invalid_status_rejected(tracker):
    session_id = tracker.create_session("Test goal")
    task_id = tracker.create_task(session_id, "Do a thing", "demo-project")
    with pytest.raises(ValueError):
        tracker.update_task_status(task_id, "NOT_REAL")


def test_task_against_missing_session_rejected(tracker):
    with pytest.raises(Exception):
        tracker.create_task("SES-DOES-NOT-EXIST", "Do a thing", "demo-project")


def test_crash_recovery_reopens_same_state(tmp_path):
    db_path = tmp_path / "test_jarvis.db"
    t1 = StateTracker(db_path=db_path)
    session_id = t1.create_session("Recoverable goal")
    task_id = t1.create_task(session_id, "Recoverable task", "demo-project")
    t1.update_task_status(task_id, "IN_PROGRESS")
    del t1  # simulate process ending

    t2 = StateTracker(db_path=db_path)  # simulate a fresh process reopening
    active = t2.get_active_tasks()
    assert any(t["task_id"] == task_id for t in active)


def test_project_registration_roundtrip(tracker):
    tracker.register_project("demo-project", "demo-project", "Demo Project")
    assert tracker.get_project_root("demo-project") == "demo-project"
