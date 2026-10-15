"""
Reproduction and integrity tests for M002.1 review.
Branch: feature/antigravity-m002-1-review
"""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.core import plan_commit
from jarvis.core.checkpoint import create_checkpoint, rollback
from jarvis.core.plan import ExecutionPlan, PlanStatus
from jarvis.core.plan_commit import _first_link_on_the_way_to, commit_plan_paths
from jarvis.state.database import get_connection
from jarvis.state.plan_store import PlanStore
from jarvis.state.tracker import StateTracker


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(workspace))
    repo = workspace / "proj"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("v1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "initial")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("OUTSIDE SECRET DATA\n")
    tracker = StateTracker(db_path=tmp_path / "state.db")
    tracker.register_project("proj", "proj")
    task_id = tracker.create_task(tracker.create_session("goal"), "t", "proj")
    store = PlanStore(tracker.db_path)
    return SimpleNamespace(repo=repo, outside=outside, tmp=tmp_path, tracker=tracker, task_id=task_id, store=store)


# ---------------------------------------------------------------------------
# Finding 1 Reproduction: Directory junction in plan paths escapes link guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dir_arg", ["sub", "sub/"])
def test_directory_junction_as_plan_path_escapes_link_guard(env, dir_arg):
    """
    Finding 1: When a plan path is a directory junction itself ('sub' or 'sub/'),
    _first_link_on_the_way_to only inspects parts[:-1] (which is empty), so it returns
    None. commit_plan_paths then proceeds, git walks through the junction into 'outside',
    and commits outside files into the repository!
    """
    if os.name != "nt":
        pytest.skip("Windows directory junctions required")

    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(env.repo / "sub"), str(env.outside)],
        capture_output=True,
    )
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")

    try:
        # _first_link_on_the_way_to returns None because parts[:-1] is empty:
        detected = _first_link_on_the_way_to(env.repo, [dir_arg])
        assert detected is None, "Demonstrates that _first_link_on_the_way_to misses the directory junction itself"

        # Calling commit_plan_paths with [dir_arg] commits outside/secret.txt!
        before_commit = git(env.repo, "rev-parse", "HEAD")
        res = commit_plan_paths("proj", "commit dir", [dir_arg], auto_confirm=True)

        assert not res.startswith("ERROR"), "commit_plan_paths did not refuse the directory junction"
        assert git(env.repo, "rev-parse", "HEAD") != before_commit
        assert "sub/secret.txt" in git(env.repo, "ls-files"), "outside secret leaked into git repo"
    finally:
        os.rmdir(env.repo / "sub")


# ---------------------------------------------------------------------------
# Finding 2 Reproduction: Rollback does not check plan_run_locks
# ---------------------------------------------------------------------------

def test_rollback_does_not_check_plan_run_locks(env):
    """
    Finding 2: Rollback restores the repo and resets plan state without checking
    if an active process owns the plan in plan_run_locks.
    """
    # Create checkpoint
    cp_id = create_checkpoint(env.tracker, env.task_id, "proj", "base")

    # Create plan linked to this checkpoint
    plan = env.store.create(ExecutionPlan.from_dict({
        "task_id": env.task_id,
        "steps": [{"tool": "write_file", "params": {"path": "proj/b.txt", "content": "hello"}}],
    }, require_state=False))
    plan.checkpoint_id = cp_id
    plan.status = PlanStatus.BLOCKED
    env.store.save(plan)

    # Advance git repo and simulate plan running
    (env.repo / "b.txt").write_text("created by running plan\n")
    git(env.repo, "add", "-A")
    git(env.repo, "commit", "-m", "step 1 committed")

    # Simulate an active plan_run_lock held by PID
    token = "simulated_token_123"
    with get_connection(env.tracker.db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS plan_run_locks ("
            "plan_id TEXT PRIMARY KEY, owner_pid INTEGER NOT NULL, owner_token TEXT NOT NULL, acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO plan_run_locks (plan_id, owner_pid, owner_token) VALUES (?, ?, ?)",
            (plan.plan_id, os.getpid(), token),
        )

    # Rollback runs and resets the plan even though plan_run_locks has an active owner!
    lines = []
    res = rollback(env.tracker, cp_id, auto_confirm=True, out=lines.append)

    assert "No tracked files to roll back" not in res
    # The plan state was reset to PENDING while the lock was still active:
    reloaded = env.store.load(plan.plan_id)
    assert reloaded.status == PlanStatus.PENDING
