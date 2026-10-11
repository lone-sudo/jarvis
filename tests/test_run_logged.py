"""Outcome classification tests for the shared execution logger."""

import sqlite3

import pytest

from jarvis.state.tracker import StateTracker
from jarvis.tools.base import run_logged


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    yield


@pytest.fixture
def tracker(tmp_path):
    return StateTracker(db_path=tmp_path / "test.db")


def _new_task(tracker):
    session_id = tracker.create_session("test execution logging")
    return tracker.create_task(session_id, "test task", "demo-project")


def _last_execution(tracker, task_id):
    with sqlite3.connect(tracker.db_path) as conn:
        return conn.execute(
            "SELECT status, output_payload FROM executions "
            "WHERE task_id = ? ORDER BY executed_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()


def test_document_quoting_policy_phrase_is_success(tracker):
    task_id = _new_task(tracker)

    run_logged(
        tracker, task_id, "read_file", "READ_ONLY",
        lambda: "The document says access denied by policy in this example.",
    )

    assert _last_execution(tracker, task_id)[0] == "SUCCESS"


def test_error_policy_denial_is_rejected(tracker):
    task_id = _new_task(tracker)

    run_logged(
        tracker, task_id, "write_file", "SAFE_WRITE",
        lambda: "ERROR: Access DeNied By Policy. x",
    )

    assert _last_execution(tracker, task_id)[0] == "REJECTED_BY_POLICY"


def test_declined_commit_is_rejected(tracker):
    task_id = _new_task(tracker)

    run_logged(
        tracker, task_id, "git_commit", "SAFE_WRITE",
        lambda: "ERROR: commit (declined by user)",
    )

    assert _last_execution(tracker, task_id)[0] == "REJECTED_BY_POLICY"


def test_other_error_is_failure(tracker):
    task_id = _new_task(tracker)

    run_logged(
        tracker, task_id, "read_file", "READ_ONLY",
        lambda: "ERROR: file not found",
    )

    assert _last_execution(tracker, task_id)[0] == "FAILURE"


def test_exception_is_logged_as_failure_and_reraised(tracker):
    task_id = _new_task(tracker)

    def explode():
        raise RuntimeError("tool crashed")

    with pytest.raises(RuntimeError, match="tool crashed"):
        run_logged(tracker, task_id, "read_file", "READ_ONLY", explode)

    status, output = _last_execution(tracker, task_id)
    assert status == "FAILURE"
    assert output == "tool crashed"


def test_non_string_result_is_success_and_logged_as_string(tracker):
    task_id = _new_task(tracker)
    result = {"ok": True}

    returned = run_logged(tracker, task_id, "inspect", "READ_ONLY", lambda: result)

    status, output = _last_execution(tracker, task_id)
    assert returned is result
    assert status == "SUCCESS"
    assert output == str(result)


def test_output_payload_is_truncated_to_2000_characters(tracker):
    task_id = _new_task(tracker)
    result = "x" * 2500

    run_logged(tracker, task_id, "read_file", "READ_ONLY", lambda: result)

    status, output = _last_execution(tracker, task_id)
    assert status == "SUCCESS"
    assert len(output) == 2000
    assert output == result[:2000]
