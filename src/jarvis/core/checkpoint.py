"""
Pre-flight checkpoints and explicit rollback (M002, ADR-0005).

A checkpoint is just a row in the existing `checkpoints` table: the
branch and HEAD commit hash at the moment a plan first runs. Nothing is
stashed, branched or copied (decision 5: no git stash/branch bloat).

The engine never rolls anything back on its own. Rollback happens only
when a human runs `jarvis rollback --checkpoint <id>`, after seeing
exactly what it will discard, and confirming [y/N].

Design consequence worth knowing: `git reset --hard <hash>` is only a
faithful "undo this plan" if nothing else was uncommitted when the plan
started. So create_checkpoint() refuses a tree with uncommitted changes
to tracked files. Untracked files don't count: reset --hard leaves them
alone, and rollback lists them rather than touching them.
"""

import uuid
from pathlib import Path

from jarvis.policy.rules import ActionTier
from jarvis.state.database import get_connection
from jarvis.state.plan_store import PlanStore, get_task_row
from jarvis.tools.git import GitInspector


class CheckpointError(Exception):
    """A checkpoint or rollback could not be created or performed. Nothing was changed."""


def _confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")


def _git(target: Path, *args: str) -> str:
    """GitInspector._run, raising on its 'ERROR: ...' string convention."""
    out = GitInspector._run(list(args), target)
    if out.startswith("ERROR"):
        raise CheckpointError(out)
    return out


def _git_target(tool_name: str, tier: ActionTier, project_root: str) -> Path:
    target, error = GitInspector._authorized_target(tool_name, tier, project_root, require_git_repo=True)
    if error:
        raise CheckpointError(error)
    return target


def create_checkpoint(tracker, task_id: str, project_root: str, notes: str = "") -> str:
    """
    Records branch + HEAD for `project_root` (workspace-relative) under
    `task_id` and returns the new checkpoint id. Raises CheckpointError,
    having changed nothing, if the project isn't a git repo with at
    least one commit, or if tracked files have uncommitted changes.
    """
    target = _git_target("git_checkpoint", ActionTier.OBSERVE, project_root)

    try:
        head = _git(target, "rev-parse", "HEAD")
    except CheckpointError as e:
        raise CheckpointError(f"Cannot checkpoint: the repository has no commits yet ({e}).") from e

    dirty = _git(target, "status", "--short", "--untracked-files=no")
    if dirty:
        raise CheckpointError(
            "Cannot checkpoint: tracked files have uncommitted changes, so a rollback to this point "
            "would not be a faithful undo of the plan. Commit (or stash) them first, then re-run.\n"
            f"{dirty}"
        )
    branch = _git(target, "branch", "--show-current")  # empty string when HEAD is detached

    checkpoint_id = f"CHK-{uuid.uuid4().hex[:8].upper()}"
    with get_connection(tracker.db_path) as conn:
        conn.execute(
            "INSERT INTO checkpoints (checkpoint_id, task_id, git_branch, git_commit_hash, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (checkpoint_id, task_id, branch, head, notes),
        )
    return checkpoint_id


def get_checkpoint(db_path, checkpoint_id: str) -> dict | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
        ).fetchone()
        return dict(row) if row else None


def rollback(tracker, checkpoint_id: str, *, auto_confirm: bool | None = None, out=print) -> str:
    """
    Restores the project to a checkpoint's commit with `git reset --hard`,
    after showing what will be discarded and asking [y/N] (auto_confirm
    exists for tests only, same convention as write_file / commit_changes).

    Safety rails, all checked before anything is touched:
      * the checkpoint and its commit must exist
      * the current branch must match the one recorded (resetting a
        different branch would silently move it)
      * policy authorization at SAFE_WRITE tier for the project directory

    Commits dropped from the branch remain in `git reflog`. Untracked
    files are never removed. Any plan that used this checkpoint is reset
    to PENDING so it cannot later resume past work that no longer exists.
    """
    cp = get_checkpoint(tracker.db_path, checkpoint_id)
    if cp is None:
        raise CheckpointError(f"No checkpoint found with id '{checkpoint_id}'.")

    task = get_task_row(tracker.db_path, cp["task_id"])
    if task is None:
        raise CheckpointError(f"Checkpoint {checkpoint_id} refers to a task that no longer exists.")
    project_root = tracker.get_project_root(task["project_key"])
    if not project_root:
        raise CheckpointError(f"Project '{task['project_key']}' is not registered.")

    target = _git_target("git_rollback", ActionTier.SAFE_WRITE, project_root)
    commit = cp["git_commit_hash"]

    try:
        _git(target, "cat-file", "-e", f"{commit}^{{commit}}")
    except CheckpointError as e:
        raise CheckpointError(f"Checkpoint commit {commit[:10]} no longer exists in this repository ({e}).") from e

    current_branch = _git(target, "branch", "--show-current")
    if current_branch != (cp["git_branch"] or ""):
        raise CheckpointError(
            f"Checkpoint was taken on branch '{cp['git_branch'] or '(detached HEAD)'}' but the repository is now on "
            f"'{current_branch or '(detached HEAD)'}'. Refusing to reset a different branch. "
            f"Switch back first (git checkout {cp['git_branch'] or commit})."
        )

    dropped = _git(target, "log", "--oneline", f"{commit}..HEAD")
    changes = _git(target, "diff", "--stat", commit)
    untracked = _git(target, "ls-files", "--others", "--exclude-standard")

    if not dropped and not changes:
        return f"Nothing to roll back: {project_root} already matches checkpoint {checkpoint_id}."

    out(f"\n--- Proposed rollback of {target} to checkpoint {checkpoint_id} ({commit[:10]}) ---")
    if dropped:
        out("Commits that will be removed from the branch (still recoverable via git reflog):")
        out(dropped)
    if changes:
        out("Changes to tracked files that will be DISCARDED:")
        out(changes)
    if untracked:
        out("Untracked files (NOT touched, listed for your information):")
        out(untracked)
    out("---")

    confirmed = auto_confirm if auto_confirm is not None else _confirm(
        f"Reset {project_root} to {commit[:10]}? This discards the changes above."
    )
    if not confirmed:
        tracker.log_execution(
            cp["task_id"], "git_rollback", "SAFE_WRITE", "REJECTED_BY_POLICY",
            input_payload=f"checkpoint={checkpoint_id}", output_payload="declined by user",
        )
        return "Rollback declined. Nothing was changed."

    try:
        _git(target, "reset", "--hard", commit)
    except CheckpointError as e:
        tracker.log_execution(
            cp["task_id"], "git_rollback", "SAFE_WRITE", "FAILURE",
            input_payload=f"checkpoint={checkpoint_id}", output_payload=str(e),
        )
        raise

    store = PlanStore(tracker.db_path)
    reset_ids = []
    for plan in store.list_for_task(cp["task_id"]):
        if plan.checkpoint_id == checkpoint_id:
            plan.reset_for_rerun()
            store.save(plan)
            reset_ids.append(plan.plan_id)

    tracker.log_execution(
        cp["task_id"], "git_rollback", "SAFE_WRITE", "SUCCESS",
        input_payload=f"checkpoint={checkpoint_id}", output_payload=f"reset to {commit}",
    )
    msg = f"Rolled back {project_root} to {commit[:10]} (checkpoint {checkpoint_id})."
    if reset_ids:
        msg += f" Plan(s) reset to PENDING: {', '.join(reset_ids)}."
    return msg
