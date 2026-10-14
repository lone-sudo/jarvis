"""
Run lock for `plan-run` (ADR-0005 amendment): one live process per plan.

The single-process tests only prove the bookkeeping. The two tests that start real child
processes are the ones that prove the guard works across processes and after a crash.
"""

import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import jarvis
from jarvis.core.engine import EngineError, ExecutionEngine
from jarvis.core.plan import ExecutionPlan, PlanStatus
from jarvis.state.plan_store import PlanLocked, PlanNotFound, PlanStore, _pid_alive
from jarvis.state.tracker import StateTracker

SRC_DIR = str(Path(jarvis.__file__).resolve().parents[1])

# Child process: claims a plan, reports, then either dies without releasing or waits to release.
CHILD = """
import os, sys
from jarvis.state.plan_store import PlanStore
store = PlanStore(sys.argv[1])
token = store.acquire_run_lock(sys.argv[2])
print("LOCKED", os.getpid(), flush=True)
if sys.argv[3] == "crash":
    os._exit(0)
sys.stdin.readline()
store.release_run_lock(sys.argv[2], token)
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "proj").mkdir(parents=True)
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(workspace))
    tracker = StateTracker(db_path=tmp_path / "s.db")
    tracker.register_project("proj", "proj")
    task = tracker.create_task(tracker.create_session("goal"), "t", "proj")
    store = PlanStore(tracker.db_path)
    plan = store.create(ExecutionPlan.from_dict(
        {"task_id": task, "steps": [{"tool": "list_directory", "params": {}}]}, require_state=False,
    ))
    lines = []
    engine = ExecutionEngine(tracker, store, verifier_factories={}, auto_confirm=True, out=lines.append)
    return SimpleNamespace(tracker=tracker, store=store, plan=plan, engine=engine, lines=lines, db=tracker.db_path)


def lock_rows(env):
    conn = sqlite3.connect(env.db)
    try:
        return conn.execute("SELECT plan_id, owner_pid FROM plan_run_locks").fetchall()
    finally:
        conn.close()


def start_child(env, mode):
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(env.db), env.plan.plan_id, mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**__import__("os").environ, "PYTHONPATH": SRC_DIR},
    )
    line = proc.stdout.readline()
    assert line.startswith("LOCKED"), f"child did not claim the plan: {line!r} {proc.stderr.read()}"
    return proc


def finished_pid():
    """A process id that belonged to a process that has already exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# ---- bookkeeping -----------------------------------------------------------

def test_second_claim_is_refused_and_release_frees_the_plan(env):
    token = env.store.acquire_run_lock(env.plan.plan_id)
    with pytest.raises(PlanLocked):
        env.store.acquire_run_lock(env.plan.plan_id)
    env.store.release_run_lock(env.plan.plan_id, token)
    assert lock_rows(env) == []
    env.store.release_run_lock(env.plan.plan_id, env.store.acquire_run_lock(env.plan.plan_id))


def test_release_with_someone_elses_token_frees_nothing(env):
    env.store.acquire_run_lock(env.plan.plan_id)
    env.store.release_run_lock(env.plan.plan_id, "not-the-owner")
    assert len(lock_rows(env)) == 1


def test_unknown_plan_is_not_found_and_leaves_no_lock(env):
    with pytest.raises(PlanNotFound):
        env.store.acquire_run_lock("PLN-nope")
    assert lock_rows(env) == []


def test_lock_row_is_removed_with_its_plan(env):
    env.store.acquire_run_lock(env.plan.plan_id)
    conn = sqlite3.connect(env.db)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM execution_plans WHERE plan_id = ?", (env.plan.plan_id,))
    conn.commit()
    conn.close()
    assert lock_rows(env) == []


def test_pid_alive_basics():
    assert _pid_alive(__import__("os").getpid()) is True
    assert _pid_alive(0) is False
    assert _pid_alive(-5) is False
    assert _pid_alive(finished_pid()) is False


def test_lock_left_by_a_dead_owner_is_taken_over(env):
    conn = sqlite3.connect(env.db)
    conn.execute(
        "INSERT INTO plan_run_locks (plan_id, owner_pid, owner_token) VALUES (?, ?, 'old')",
        (env.plan.plan_id, finished_pid()),
    )
    conn.commit()
    conn.close()
    token = env.store.acquire_run_lock(env.plan.plan_id)
    assert token != "old"
    assert [r[1] for r in lock_rows(env)] == [__import__("os").getpid()]


# ---- real processes -----------------------------------------------------------

def test_a_live_process_blocks_a_second_one_until_it_releases(env):
    child = start_child(env, "hold")
    try:
        with pytest.raises(PlanLocked) as err:
            env.store.acquire_run_lock(env.plan.plan_id)
        assert str(child.pid) in str(err.value)
        with pytest.raises(EngineError):
            env.engine.run(env.plan.plan_id)
    finally:
        child.communicate(input="\n", timeout=30)
    assert child.returncode == 0
    assert lock_rows(env) == []
    env.store.release_run_lock(env.plan.plan_id, env.store.acquire_run_lock(env.plan.plan_id))


def test_a_process_that_dies_without_releasing_does_not_block_the_plan_forever(env):
    child = start_child(env, "crash")
    child.wait(timeout=30)
    assert len(lock_rows(env)) == 1, "the crashed child must have left its lock behind"
    env.store.release_run_lock(env.plan.plan_id, env.store.acquire_run_lock(env.plan.plan_id))
    assert env.engine.run(env.plan.plan_id).status == PlanStatus.COMPLETED


@pytest.mark.parametrize("round_number", range(10))
def test_simultaneous_claims_have_exactly_one_winner(env, round_number):
    barrier = threading.Barrier(8)
    outcomes = []

    def attempt():
        barrier.wait()
        try:
            outcomes.append(("won", env.store.acquire_run_lock(env.plan.plan_id)))
        except PlanLocked:
            outcomes.append(("refused", None))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(o[0] for o in outcomes) == ["refused"] * 7 + ["won"]
    assert len(lock_rows(env)) == 1


# ---- the engine ------------------------------------------------------------------

def test_engine_refuses_a_plan_another_process_is_running_and_changes_nothing(env):
    env.store.acquire_run_lock(env.plan.plan_id)  # stands in for the other live run
    with pytest.raises(EngineError, match="already being run"):
        env.engine.run(env.plan.plan_id)
    assert env.store.load(env.plan.plan_id).status == PlanStatus.PENDING
    assert env.lines == []
    assert len(lock_rows(env)) == 1


def test_engine_releases_the_lock_after_a_completed_run(env):
    assert env.engine.run(env.plan.plan_id).status == PlanStatus.COMPLETED
    assert lock_rows(env) == []


def test_engine_releases_the_lock_when_the_run_raises(env, monkeypatch):
    def boom(_plan_id):
        raise RuntimeError("engine bug")

    monkeypatch.setattr(env.engine, "_run_locked", boom)
    with pytest.raises(RuntimeError):
        env.engine.run(env.plan.plan_id)
    assert lock_rows(env) == []


def test_engine_unknown_plan_still_raises_plan_not_found(env):
    with pytest.raises(PlanNotFound):
        env.engine.run("PLN-nope")
