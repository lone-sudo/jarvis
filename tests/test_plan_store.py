import json
import sqlite3

import pytest

from jarvis.core.plan import ExecutionPlan, ExecutionStep, PlanFormatError, PlanStatus, StepStatus
from jarvis.state.plan_store import PlanNotFound, PlanStore, get_task_row
from jarvis.state.tracker import StateTracker


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "plans.db"
    tracker = StateTracker(db_path=db)
    session = tracker.create_session("goal")
    task = tracker.create_task(session, "a task", "demo")
    return tracker, PlanStore(db_path=db), task, db


def _plan(task_id, n=1):
    return ExecutionPlan(
        task_id=task_id,
        steps=[ExecutionStep(tool="read_file", params={"path": f"{i}.txt"}) for i in range(n)],
    )


def test_migration_005_creates_table_and_advances_version(env):
    _, _, _, db = env
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "execution_plans" in tables
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] >= 5
    cols = {r[1] for r in conn.execute("PRAGMA table_info(execution_plans)")}
    assert cols == {"plan_id", "task_id", "status", "current_step_index", "max_steps",
                    "plan_json", "created_at", "updated_at"}


def test_create_and_load_roundtrip(env):
    _, store, task, _ = env
    plan = store.create(_plan(task, n=2))
    assert store.load(plan.plan_id) == plan


def test_columns_stay_in_sync_with_plan_json_on_save(env):
    _, store, task, db = env
    plan = store.create(_plan(task, n=3))
    plan.status = PlanStatus.BLOCKED
    plan.current_step_index = 2
    plan.steps[0].status = StepStatus.SUCCEEDED
    store.save(plan)

    row = sqlite3.connect(db).execute(
        "SELECT status, current_step_index, max_steps, plan_json FROM execution_plans WHERE plan_id=?",
        (plan.plan_id,),
    ).fetchone()
    assert row[0] == "BLOCKED" and row[1] == 2 and row[2] == plan.max_steps
    assert json.loads(row[3])["status"] == "BLOCKED"
    assert store.load(plan.plan_id).current_step_index == 2


def test_create_requires_existing_task(env):
    _, store, _, _ = env
    with pytest.raises(ValueError, match="No task found"):
        store.create(_plan("TSK-NOPE0000"))


def test_create_rejects_malformed_plan(env):
    _, store, task, _ = env
    bad = _plan(task, n=2)
    bad.max_steps = 1
    with pytest.raises(PlanFormatError):
        store.create(bad)


def test_load_and_save_unknown_plan(env):
    _, store, task, _ = env
    with pytest.raises(PlanNotFound):
        store.load("PLN-NOPE0000")
    with pytest.raises(PlanNotFound):
        store.save(_plan(task))


def test_corrupt_stored_plan_raises_clearly(env):
    _, store, task, db = env
    plan = store.create(_plan(task))
    conn = sqlite3.connect(db)
    conn.execute("UPDATE execution_plans SET plan_json = '{broken' WHERE plan_id = ?", (plan.plan_id,))
    conn.commit()
    with pytest.raises(PlanFormatError, match="corrupt"):
        store.load(plan.plan_id)


def test_list_for_task_orders_by_creation(env):
    _, store, task, _ = env
    a = store.create(_plan(task))
    b = store.create(_plan(task))
    assert [p.plan_id for p in store.list_for_task(task)] == [a.plan_id, b.plan_id]
    assert store.list_for_task("TSK-OTHER000") == []


def test_cascade_delete_with_task(env):
    _, store, task, db = env
    store.create(_plan(task))
    from jarvis.state.database import get_connection
    with get_connection(db) as conn:
        conn.execute("DELETE FROM tasks WHERE task_id = ?", (task,))
    assert store.list_for_task(task) == []


def test_get_task_row(env):
    _, _, task, db = env
    assert get_task_row(db, task)["title"] == "a task"
    assert get_task_row(db, "TSK-NOPE0000") is None


def test_get_failure_diagnostics_matches_plan_in_python_not_by_pattern(env):
    tracker, store, task, _ = env
    p1 = store.create(_plan(task))
    p2 = store.create(_plan(task))
    tracker.log_execution(task, "engine:failure", "READ_ONLY", "FAILURE",
                          input_payload=json.dumps({"plan_id": p1.plan_id, "step_index": 0}),
                          output_payload=json.dumps({"message": "first"}))
    tracker.log_execution(task, "engine:failure", "READ_ONLY", "FAILURE",
                          input_payload=json.dumps({"plan_id": p2.plan_id, "step_index": 0}),
                          output_payload=json.dumps({"message": "other plan"}))
    tracker.log_execution(task, "engine:failure", "READ_ONLY", "FAILURE",
                          input_payload=json.dumps({"plan_id": p1.plan_id, "step_index": 1}),
                          output_payload=json.dumps({"message": "latest for p1"}))
    assert store.get_failure_diagnostics(p1.plan_id, task)["message"] == "latest for p1"
    assert store.get_failure_diagnostics(p2.plan_id, task)["message"] == "other plan"
    assert store.get_failure_diagnostics("PLN-NONE0000", task) is None
