import sqlite3
import subprocess

import pytest

from jarvis.core import checkpoint as cp
from jarvis.core.plan import ExecutionPlan, ExecutionStep, PlanStatus, StepStatus
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

    tracker = StateTracker(db_path=tmp_path / "s.db")
    tracker.register_project("proj", "proj")
    session = tracker.create_session("goal")
    task = tracker.create_task(session, "t", "proj")
    return tracker, PlanStore(tracker.db_path), task, repo


def _executions(tracker, task):
    conn = sqlite3.connect(tracker.db_path)
    return conn.execute(
        "SELECT tool_name, status FROM executions WHERE task_id=? ORDER BY rowid", (task,)
    ).fetchall()


def test_create_checkpoint_records_head_and_branch(env):
    tracker, _, task, repo = env
    cid = cp.create_checkpoint(tracker, task, "proj", notes="plan PLN-1")
    row = cp.get_checkpoint(tracker.db_path, cid)
    assert cid.startswith("CHK-")
    assert row["git_commit_hash"] == git(repo, "rev-parse", "HEAD")
    assert row["git_branch"] == "main"
    assert row["task_id"] == task and row["notes"] == "plan PLN-1"


def test_checkpoint_refuses_uncommitted_tracked_changes(env):
    tracker, _, task, repo = env
    (repo / "a.txt").write_text("dirty\n")
    with pytest.raises(cp.CheckpointError, match="uncommitted"):
        cp.create_checkpoint(tracker, task, "proj")
    assert sqlite3.connect(tracker.db_path).execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0


def test_checkpoint_allows_untracked_files(env):
    tracker, _, task, repo = env
    (repo / "scratch.tmp").write_text("junk")
    assert cp.create_checkpoint(tracker, task, "proj").startswith("CHK-")


def test_checkpoint_refuses_repo_without_commits(env, tmp_path):
    tracker, _, task, _ = env
    empty = tmp_path / "workspace" / "empty"
    empty.mkdir()
    git(empty, "init", "-b", "main")
    tracker.register_project("empty", "empty")
    with pytest.raises(cp.CheckpointError, match="no commits"):
        cp.create_checkpoint(tracker, task, "empty")


def test_checkpoint_refuses_non_git_directory(env, tmp_path):
    tracker, _, task, _ = env
    (tmp_path / "workspace" / "plain").mkdir()
    with pytest.raises(cp.CheckpointError, match="Not a git repository"):
        cp.create_checkpoint(tracker, task, "plain")


def test_rollback_restores_files_and_drops_later_commits(env):
    tracker, _, task, repo = env
    cid = cp.create_checkpoint(tracker, task, "proj")
    original = git(repo, "rev-parse", "HEAD")

    (repo / "a.txt").write_text("v2\n")
    git(repo, "commit", "-am", "plan commit")
    (repo / "a.txt").write_text("v3 uncommitted\n")
    (repo / "keep.txt").write_text("untracked\n")

    lines = []
    msg = cp.rollback(tracker, cid, auto_confirm=True, out=lines.append)

    assert (repo / "a.txt").read_text() == "v1\n"
    assert git(repo, "rev-parse", "HEAD") == original
    assert (repo / "keep.txt").read_text() == "untracked\n"  # untracked files are never touched
    assert "Rolled back" in msg
    shown = "\n".join(lines)
    assert "plan commit" in shown and "keep.txt" in shown and "a.txt" in shown  # preview told the truth
    assert _executions(tracker, task)[-1] == ("git_rollback", "SUCCESS")


def test_rollback_decline_changes_nothing(env):
    tracker, _, task, repo = env
    cid = cp.create_checkpoint(tracker, task, "proj")
    (repo / "a.txt").write_text("v2\n")
    git(repo, "commit", "-am", "later")
    head = git(repo, "rev-parse", "HEAD")

    msg = cp.rollback(tracker, cid, auto_confirm=False, out=lambda *_: None)

    assert "declined" in msg.lower()
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / "a.txt").read_text() == "v2\n"
    assert _executions(tracker, task)[-1] == ("git_rollback", "REJECTED_BY_POLICY")


def test_rollback_prompts_when_not_auto_confirmed(env, monkeypatch):
    tracker, _, task, repo = env
    cid = cp.create_checkpoint(tracker, task, "proj")
    (repo / "a.txt").write_text("v2\n")
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or "n")
    cp.rollback(tracker, cid, out=lambda *_: None)
    assert len(prompts) == 1 and "[y/N]" in prompts[0]
    assert (repo / "a.txt").read_text() == "v2\n"


def test_rollback_refuses_different_branch(env):
    tracker, _, task, repo = env
    cid = cp.create_checkpoint(tracker, task, "proj")
    git(repo, "checkout", "-b", "other")
    (repo / "a.txt").write_text("on other\n")
    git(repo, "commit", "-am", "other work")
    head = git(repo, "rev-parse", "HEAD")

    with pytest.raises(cp.CheckpointError, match="different branch"):
        cp.rollback(tracker, cid, auto_confirm=True, out=lambda *_: None)
    assert git(repo, "rev-parse", "HEAD") == head


def test_rollback_when_already_at_checkpoint(env):
    tracker, _, task, _ = env
    cid = cp.create_checkpoint(tracker, task, "proj")
    assert "Nothing to roll back" in cp.rollback(tracker, cid, auto_confirm=True, out=lambda *_: None)


def test_rollback_unknown_checkpoint(env):
    tracker, _, _, _ = env
    with pytest.raises(cp.CheckpointError, match="No checkpoint found"):
        cp.rollback(tracker, "CHK-NOPE0000", auto_confirm=True)


def test_rollback_with_missing_commit(env):
    tracker, _, task, _ = env
    conn = sqlite3.connect(tracker.db_path)
    conn.execute(
        "INSERT INTO checkpoints (checkpoint_id, task_id, git_branch, git_commit_hash) VALUES (?,?,?,?)",
        ("CHK-BADC0DE0", task, "main", "0" * 40),
    )
    conn.commit()
    with pytest.raises(cp.CheckpointError, match="no longer exists"):
        cp.rollback(tracker, "CHK-BADC0DE0", auto_confirm=True)


def test_rollback_resets_only_plans_that_used_the_checkpoint(env):
    tracker, store, task, repo = env
    cid = cp.create_checkpoint(tracker, task, "proj")

    def blocked_plan(checkpoint_id):
        p = ExecutionPlan(task_id=task, steps=[ExecutionStep(tool="read_file", params={"path": "a.txt"})])
        p.status, p.current_step_index, p.checkpoint_id = PlanStatus.BLOCKED, 1, checkpoint_id
        p.steps[0].status, p.steps[0].result, p.blocked_reason = StepStatus.SUCCEEDED, "out", "x"
        return store.create(p)

    mine, other = blocked_plan(cid), blocked_plan("CHK-OTHER000")
    (repo / "a.txt").write_text("v2\n")

    msg = cp.rollback(tracker, cid, auto_confirm=True, out=lambda *_: None)

    reloaded = store.load(mine.plan_id)
    assert reloaded.status is PlanStatus.PENDING and reloaded.current_step_index == 0
    assert reloaded.checkpoint_id is None and reloaded.blocked_reason == ""
    assert reloaded.steps[0].status is StepStatus.PENDING and reloaded.steps[0].result == ""
    assert store.load(other.plan_id).status is PlanStatus.BLOCKED
    assert mine.plan_id in msg
