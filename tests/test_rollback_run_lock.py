"""
`jarvis rollback` and the plan run lock (Antigravity's review of M002.1, finding 2).

A rollback used to reset the repository and the plan state while another process was still
running that plan, so the run kept writing over a tree it believed was its own. Now rollback takes
the run lock of every plan that used the checkpoint: a live run refuses it, and no run can start
while it waits at [y/N]. The live-owner tests use a real child process.
"""

import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import jarvis
from jarvis.core import checkpoint as checkpoint_module
from jarvis.core.checkpoint import CheckpointError, create_checkpoint, rollback
from jarvis.core.plan import ExecutionPlan, PlanStatus
from jarvis.state.plan_store import PlanLocked, PlanStore
from jarvis.state.tracker import StateTracker

SRC_DIR = str(Path(jarvis.__file__).resolve().parents[1])

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


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "proj"
    repo.mkdir(parents=True)
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(workspace))
    git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("v1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "initial")
    tracker = StateTracker(db_path=tmp_path / "s.db")
    tracker.register_project("proj", "proj")
    task = tracker.create_task(tracker.create_session("goal"), "t", "proj")
    store = PlanStore(tracker.db_path)
    cp_id = create_checkpoint(tracker, task, "proj", "base")
    plan = store.create(ExecutionPlan.from_dict(
        {"task_id": task, "steps": [{"tool": "list_directory", "params": {}}]}, require_state=False,
    ))
    plan.checkpoint_id = cp_id
    plan.status = PlanStatus.BLOCKED
    store.save(plan)
    (repo / "b.txt").write_text("written by the plan\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "step 1")
    return SimpleNamespace(tracker=tracker, store=store, plan=plan, cp=cp_id, repo=repo, db=tracker.db_path)


def lock_rows(env):
    conn = sqlite3.connect(env.db)
    try:
        return conn.execute("SELECT plan_id FROM plan_run_locks").fetchall()
    finally:
        conn.close()


def start_child(env, mode):
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(env.db), env.plan.plan_id, mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env={"PYTHONPATH": SRC_DIR, "PATH": ""},
    )
    assert child.stdout.readline().startswith("LOCKED")
    return child


def test_rollback_refuses_while_another_process_is_running_the_plan(env):
    child = start_child(env, "wait")
    try:
        with pytest.raises(CheckpointError, match="running right now"):
            rollback(env.tracker, env.cp, auto_confirm=True, out=lambda _line: None)
        # nothing was touched: the commit the plan made is still there and the plan is still BLOCKED
        assert (env.repo / "b.txt").exists()
        assert env.store.load(env.plan.plan_id).status is PlanStatus.BLOCKED
    finally:
        child.communicate("\n", timeout=20)


def test_rollback_goes_ahead_when_the_lock_owner_is_dead(env):
    child = start_child(env, "crash")
    child.wait(timeout=20)
    rollback(env.tracker, env.cp, auto_confirm=True, out=lambda _line: None)
    assert not (env.repo / "b.txt").exists()
    assert env.store.load(env.plan.plan_id).status is PlanStatus.PENDING


def test_rollback_releases_its_locks_after_success(env):
    rollback(env.tracker, env.cp, auto_confirm=True, out=lambda _line: None)
    assert lock_rows(env) == []


def test_rollback_releases_its_locks_when_declined(env):
    result = rollback(env.tracker, env.cp, auto_confirm=False, out=lambda _line: None)
    assert "declined" in result.lower()
    assert lock_rows(env) == []
    assert (env.repo / "b.txt").exists()


def test_rollback_releases_its_locks_when_it_raises(env, monkeypatch):
    def boom(*_args, **_kwargs):
        raise CheckpointError("simulated failure")
    monkeypatch.setattr(checkpoint_module, "_rollback_snapshot", boom)
    with pytest.raises(CheckpointError, match="simulated failure"):
        rollback(env.tracker, env.cp, auto_confirm=True, out=lambda _line: None)
    assert lock_rows(env) == []


def test_a_run_cannot_start_while_rollback_waits_at_the_prompt(env, monkeypatch):
    seen = {}

    def prompt(_text):
        try:
            env.store.acquire_run_lock(env.plan.plan_id)
            seen["started"] = True
        except PlanLocked:
            seen["started"] = False
        return False

    monkeypatch.setattr(checkpoint_module, "_confirm", prompt)
    rollback(env.tracker, env.cp, out=lambda _line: None)
    assert seen == {"started": False}
    assert lock_rows(env) == []


def test_a_plan_from_another_checkpoint_does_not_block_this_rollback(env):
    other = env.store.create(ExecutionPlan.from_dict(
        {"task_id": env.plan.task_id, "steps": [{"tool": "list_directory", "params": {}}]}, require_state=False,
    ))
    token = env.store.acquire_run_lock(other.plan_id)
    try:
        rollback(env.tracker, env.cp, auto_confirm=True, out=lambda _line: None)
        assert not (env.repo / "b.txt").exists()
    finally:
        env.store.release_run_lock(other.plan_id, token)
