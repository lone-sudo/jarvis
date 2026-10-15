"""
`task-done` and the DONE confirmation (M002.1, ADR-0005 (e), AGENTS.md invariant 3).

DONE is terminal, so every CLI path that sets it must show what is being closed and ask [y/N].
The key tests are the ones through the CLI: `ask-ai --done` must not be a back door.
"""

import sqlite3
import sys
from types import SimpleNamespace

import pytest

from jarvis.core.engine import ExecutionEngine
from jarvis.core.plan import ExecutionPlan, PlanStatus
from jarvis.core.task_completion import complete_task
from jarvis.interface.cli import main
from jarvis.state.plan_store import PlanStore
from jarvis.state.tracker import StateTracker


@pytest.fixture
def env(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "proj").mkdir(parents=True)
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(workspace))
    tracker = StateTracker()
    tracker.register_project("proj", "proj")
    session = tracker.create_session("goal")

    def new_task(status="IN_PROGRESS"):
        task_id = tracker.create_task(session, "write the report", "proj")
        if status != "PENDING":
            tracker.update_task_status(task_id, "IN_PROGRESS")
            if status != "IN_PROGRESS":
                tracker.update_task_status(task_id, status)
        return task_id

    def status_of(task_id):
        conn = sqlite3.connect(tracker.db_path)
        try:
            return conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()[0]
        finally:
            conn.close()

    def logged(task_id):
        conn = sqlite3.connect(tracker.db_path)
        try:
            return conn.execute(
                "SELECT tool_name, action_type, status FROM executions WHERE task_id = ? ORDER BY rowid",
                (task_id,),
            ).fetchall()
        finally:
            conn.close()

    def add_plan(task_id):
        return PlanStore(tracker.db_path).create(ExecutionPlan.from_dict(
            {"task_id": task_id, "steps": [{"tool": "list_directory", "params": {}}]}, require_state=False,
        ))

    return SimpleNamespace(
        tracker=tracker, new_task=new_task, status_of=status_of, logged=logged, add_plan=add_plan,
    )


def no_prompt(monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("a prompt was shown where none was expected")
    monkeypatch.setattr("builtins.input", fail)


def run_cli(*argv):
    sys.argv = ["jarvis", *argv]
    main()


# ---- complete_task ------------------------------------------------------------

def test_confirmed_marks_done(env):
    task = env.new_task()
    lines = []
    result = complete_task(env.tracker, task, auto_confirm=True, out=lines.append)
    assert result == f"Task {task} is now DONE."
    assert env.status_of(task) == "DONE"
    assert any("write the report" in line for line in lines)
    assert any("permanent" in line for line in lines)


def test_declined_leaves_the_task_alone(env):
    task = env.new_task()
    result = complete_task(env.tracker, task, auto_confirm=False, out=lambda _l: None)
    assert result.startswith("ERROR:") and "(declined by user)" in result
    assert env.status_of(task) == "IN_PROGRESS"


@pytest.mark.parametrize("answer,expected", [("y", "DONE"), ("YES", "DONE"), ("n", "IN_PROGRESS"), ("", "IN_PROGRESS"), ("yep", "IN_PROGRESS")])
def test_the_real_prompt_defaults_to_no(env, monkeypatch, answer, expected):
    task = env.new_task()
    monkeypatch.setattr("builtins.input", lambda *_a: answer)
    complete_task(env.tracker, task, out=lambda _l: None)
    assert env.status_of(task) == expected


def test_awaiting_user_can_be_closed(env):
    task = env.new_task("AWAITING_USER")
    assert complete_task(env.tracker, task, auto_confirm=True, out=lambda _l: None).endswith("is now DONE.")


def test_already_done_is_idempotent_and_does_not_prompt(env, monkeypatch):
    task = env.new_task()
    complete_task(env.tracker, task, auto_confirm=True, out=lambda _l: None)
    no_prompt(monkeypatch)
    result = complete_task(env.tracker, task, out=lambda _l: None)
    assert "already DONE" in result and not result.startswith("ERROR:")
    assert env.status_of(task) == "DONE"


@pytest.mark.parametrize("status", ["PENDING", "BLOCKED", "ABORTED"])
def test_states_that_cannot_reach_done_are_refused_before_any_prompt(env, monkeypatch, status):
    task = env.new_task(status)
    no_prompt(monkeypatch)
    result = complete_task(env.tracker, task, out=lambda _l: None)
    assert result.startswith("ERROR:") and "cannot be marked DONE" in result
    assert env.status_of(task) == status


def test_unknown_task_is_an_error_and_does_not_prompt(env, monkeypatch):
    no_prompt(monkeypatch)
    assert complete_task(env.tracker, "TSK-NOPE", out=lambda _l: None).startswith("ERROR: No task found")


def test_summary_lists_plans_and_warns_about_unfinished_ones(env):
    task = env.new_task()
    plan = env.add_plan(task)
    lines = []
    complete_task(env.tracker, task, auto_confirm=False, out=lines.append)
    text = "\n".join(lines)
    assert plan.plan_id in text and "PENDING" in text
    assert "WARNING: 1 plan(s) not COMPLETED" in text


def test_no_warning_when_every_plan_is_completed(env):
    task = env.new_task()
    plan = env.add_plan(task)
    engine = ExecutionEngine(env.tracker, PlanStore(env.tracker.db_path), verifier_factories={},
                             auto_confirm=True, out=lambda _l: None)
    assert engine.run(plan.plan_id).status == PlanStatus.COMPLETED
    lines = []
    complete_task(env.tracker, task, auto_confirm=False, out=lines.append)
    assert not any("WARNING" in line for line in lines)


def test_a_completed_plan_does_not_complete_its_task(env):
    """The task may have other work outstanding, so only an explicit, confirmed task-done closes it."""
    task = env.new_task("PENDING")
    plan = env.add_plan(task)
    engine = ExecutionEngine(env.tracker, PlanStore(env.tracker.db_path), verifier_factories={},
                             auto_confirm=True, out=lambda _l: None)
    assert engine.run(plan.plan_id).status == PlanStatus.COMPLETED
    assert env.status_of(task) == "IN_PROGRESS"


# ---- through the CLI -----------------------------------------------------------

def test_cli_task_done_yes_marks_done_and_logs_success(env, monkeypatch, capsys):
    task = env.new_task()
    monkeypatch.setattr("builtins.input", lambda *_a: "y")
    run_cli("task-done", task)
    assert env.status_of(task) == "DONE"
    assert env.logged(task) == [("task_done", "SAFE_WRITE", "SUCCESS")]
    assert "is now DONE" in capsys.readouterr().out


def test_cli_task_done_no_leaves_the_task_and_logs_a_rejection(env, monkeypatch):
    task = env.new_task()
    monkeypatch.setattr("builtins.input", lambda *_a: "n")
    run_cli("task-done", task)
    assert env.status_of(task) == "IN_PROGRESS"
    assert env.logged(task) == [("task_done", "SAFE_WRITE", "REJECTED_BY_POLICY")]


def test_cli_task_done_unknown_task_prints_an_error_and_logs_nothing(env, monkeypatch, capsys):
    no_prompt(monkeypatch)
    run_cli("task-done", "TSK-NOPE")
    assert "ERROR: No task found" in capsys.readouterr().out


def _fake_provider(monkeypatch):
    provider = SimpleNamespace(dispatch_prompt=lambda prompt: "the answer")
    monkeypatch.setattr("jarvis.interface.cli._select_backend", lambda backend, label: provider)


def test_ask_ai_done_is_not_a_back_door_declining_keeps_the_task_open(env, monkeypatch, capsys):
    task = env.new_task()
    _fake_provider(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *_a: "n")
    run_cli("ask-ai", "--task", task, "--done", "hello")
    assert env.status_of(task) == "IN_PROGRESS"
    out = capsys.readouterr().out
    assert "the answer" in out and "declined by user" in out
    assert ("task_done", "SAFE_WRITE", "REJECTED_BY_POLICY") in env.logged(task)


def test_ask_ai_done_confirmed_marks_done_after_showing_the_response(env, monkeypatch, capsys):
    task = env.new_task()
    _fake_provider(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *_a: "y")
    run_cli("ask-ai", "--task", task, "--done", "hello")
    assert env.status_of(task) == "DONE"
    out = capsys.readouterr().out
    assert out.index("the answer") < out.index("is now DONE")
    assert ("task_done", "SAFE_WRITE", "SUCCESS") in env.logged(task)


def test_ask_ai_without_done_never_prompts_and_stays_in_progress(env, monkeypatch, capsys):
    task = env.new_task()
    _fake_provider(monkeypatch)
    no_prompt(monkeypatch)
    run_cli("ask-ai", "--task", task, "hello")
    assert env.status_of(task) == "IN_PROGRESS"
    assert "is now: IN_PROGRESS" in capsys.readouterr().out
