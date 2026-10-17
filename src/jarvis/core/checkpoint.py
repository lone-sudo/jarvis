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

The flip side: a file that becomes TRACKED after the checkpoint (added by a
commit) is deleted from disk by reset --hard, because the checkpoint commit
did not contain it. A plan's git_commit therefore commits only files that
plan wrote (core/plan_commit.py), and the rollback preview names every file
it will delete, so nothing disappears without being listed.
"""

import hashlib
import os
import uuid
from pathlib import Path

from jarvis.core.plan import PlanStatus
from jarvis.policy.rules import ActionTier
from jarvis.state.database import get_connection
from jarvis.state.plan_store import PlanLocked, PlanStore, get_task_row
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


def _diff_records(target: Path, commit: str, *extra: str) -> list[tuple[str, str]]:
    """
    `git diff --raw -z` as (metadata, path) pairs. Parsing the raw format rather than `--name-only`
    matters twice over: every record starts with ':' so GitInspector._run's `.strip()` can never eat
    the first file name's leading spaces (review of 524e588, F3), and it carries the file modes
    without ever serialising file content (a 350 MiB binary made `diff --binary` time out, F4).
    """
    out = _git(target, "diff", "--raw", "--no-abbrev", "--no-renames", "-z", *extra)
    tokens = out.split("\0")
    records: list[tuple[str, str]] = []
    i = 0
    while i + 1 < len(tokens):
        if tokens[i].startswith(":"):
            records.append((tokens[i], tokens[i + 1]))
            i += 2
        else:
            i += 1
    return records


def _is_link(path: Path) -> bool:
    """A symlink, or a Windows directory junction (which `is_symlink()` reports as False)."""
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _link_marker(path: Path) -> bytes:
    try:
        return os.readlink(path).encode("utf-8", errors="replace")
    except OSError:
        return b"?"


def _hash_one_path(digest, target: Path, name: str) -> None:
    path = target / name
    # Never read THROUGH a link. If any parent directory of the file is a link (a junction swapped in
    # for `sub/`), opening `sub/f.txt` would read a file outside the project: hash where the link
    # points instead and do not open anything (review of 524e588, F1a).
    parent = target
    for part in Path(name).parts[:-1]:
        parent = parent / part
        if _is_link(parent):
            digest.update(b"X" + _link_marker(parent))
            return
    try:
        if _is_link(path):                      # a link at the path itself: hash where it points
            digest.update(b"L" + _link_marker(path))
        elif path.is_file():
            digest.update(b"F")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
        elif path.is_dir():
            digest.update(b"D")                 # a directory where a file used to be (F1b)
        else:
            digest.update(b"M")                 # missing, or not a regular file
    except OSError as e:
        digest.update(b"E" + str(e.errno).encode())


def _raw_content_digest(target: Path, commit: str) -> str:
    """
    SHA-256 over the RAW BYTES on disk of every tracked file `reset --hard` can overwrite: the files
    that differ from the checkpoint in the working tree, in the index, or in HEAD.

    Why not let git do it: with core.autocrlf=true git normalises CRLF to LF before diffing, so an
    edit that only changes line endings is invisible to `git diff` (Task 006 review, F3). Forcing
    `-c core.autocrlf=false` onto `git diff` fixes that but makes the output depend on whether the
    index entry is "racy": git trusts the index blob while the entry is stat-clean and reads the
    working tree otherwise, and an ordinary `git status` flips that between two snapshots, which
    produced a false "repository changed" on the ZBook. Reading the bytes ourselves is immune to both.

    HEAD is included because a step may have committed to a file and the user then staged it back to
    the checkpoint's content: neither the working tree nor the index differs from the checkpoint,
    yet `reset --hard` still rewrites the file (review of 524e588, F2).
    """
    names: set[str] = set()
    for extra in ((commit,), ("--cached", commit), (commit, "HEAD")):
        names.update(path for _, path in _diff_records(target, commit, *extra))
    digest = hashlib.sha256()
    for name in sorted(names):
        digest.update(name.encode("utf-8", errors="replace") + b"\0")
        _hash_one_path(digest, target, name)
        digest.update(b"\0")
    return digest.hexdigest()


def _tracked_state_digest(target: Path, commit: str) -> str:
    """
    A digest of git's own view, WITHOUT any file content: which paths differ from the checkpoint, how
    (status letter and modes, so an exec-bit change shows) and, for the index, which blob is staged.
    The working-tree blob id is deliberately left out: git fills it in only when the index entry is
    stat-clean, which is the same racy-index flip as above.
    """
    lines = []
    for meta, path in _diff_records(target, commit, commit):
        fields = meta.split()                       # :oldmode newmode oldsha newsha status
        lines.append(f"W {fields[0]} {fields[1]} {fields[4]} {path}")
    for meta, path in _diff_records(target, commit, "--cached", commit):
        fields = meta.split()
        lines.append(f"I {fields[0]} {fields[1]} {fields[3]} {fields[4]} {path}")
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8", errors="replace")).hexdigest()


def _refuse_if_tracked_folder_is_a_link(target: Path) -> None:
    """
    Git for Windows treats a directory junction as an ordinary directory and walks into it, so with a
    tracked folder replaced by a junction `git diff` reads, and `git reset --hard` WRITES, files
    outside the project (review 2 of the digest, F1: reproduced on the ZBook). A POSIX symlink is
    safe because git records it as a link, not as a folder. Neither git's output nor our digest can
    be trusted across such a folder, so refuse before running either. `ls-files --stage` is parsed
    because every record starts with a digit: `_run` strips its output and would eat a leading space.
    """
    out = _git(target, "ls-files", "-z", "--stage")
    folders: set[str] = set()
    for record in out.split("\0"):
        if "\t" not in record:
            continue
        parts = Path(record.split("\t", 1)[1]).parts[:-1]
        for depth in range(1, len(parts) + 1):
            folders.add("/".join(parts[:depth]))
    for folder in sorted(folders):
        if _is_link(target / folder):
            raise CheckpointError(
                f"Refusing to roll back: the tracked folder '{folder}' is now a link or junction. Git would "
                "read and overwrite files through it, possibly outside the project. Nothing was changed. "
                "Remove the link and restore the folder (git checkout -- " + folder + "), then run the "
                "rollback again."
            )


def _rollback_snapshot(target: Path, commit: str) -> tuple[str, str, str, str, str, str, str, str]:
    """
    Everything the rollback preview is built from, in one tuple, so it can be taken
    twice (at preview time and again just before `reset --hard`) and compared.
    (commits to drop, tracked-change summary, untracked files, files reset will delete,
     HEAD, branch, digest of the exact tracked content, index/worktree status)

    The summary (`--stat`) is for humans and only counts lines, so an edit that keeps the
    line counts equal looks identical (Antigravity, Task 005 F1). The digest changes on any
    byte difference between the working tree and the checkpoint, any mode or staged-blob
    difference, and never serialises file content through git (review of 524e588, F4). The branch name matters because `reset --hard` moves whichever branch is
    checked out now, even when HEAD is the same commit (F2).
    """
    _refuse_if_tracked_folder_is_a_link(target)    # before ANY diff: git would read through the link
    dropped = _git(target, "log", "--oneline", f"{commit}..HEAD")
    changes = _git(target, "diff", "--stat", commit)
    # `diff <commit>` compares the WORKING TREE with the checkpoint. The index can differ while
    # the working tree matches (an edit staged, then reverted on disk), and `reset --hard` resets
    # the index too, so that difference must be listed and must stop a "nothing to roll back".
    staged = _git(target, "diff", "--cached", "--stat", commit)
    if staged and staged != changes:
        changes = (changes + "\nStaged in the index and differing from the checkpoint:\n" + staged).strip()
    untracked = _git(target, "ls-files", "--others", "--exclude-standard")
    # Tracked now, absent from the checkpoint commit: reset --hard will delete these from disk.
    deleted = _git(target, "diff", "--name-only", "--diff-filter=A", commit)
    head = _git(target, "rev-parse", "HEAD")
    branch = _git(target, "branch", "--show-current")
    digest = _tracked_state_digest(target, commit) + ":" + _raw_content_digest(target, commit)
    status = _git(target, "status", "--porcelain=v2", "-z", "--untracked-files=no")
    return dropped, changes, untracked, deleted, head, branch, digest, status


def _refuse_if_changed(tracker, cp_task_id, checkpoint_id, target, commit, snapshot) -> None:
    """The preview the human confirmed is only honest if nothing changed while the prompt was open."""
    try:
        unchanged = _rollback_snapshot(target, commit) == snapshot
    except CheckpointError as e:
        # Cannot tell whether anything changed (git error or timeout): fail closed, and leave a trace.
        tracker.log_execution(
            cp_task_id, "git_rollback", "SAFE_WRITE", "FAILURE",
            input_payload=f"checkpoint={checkpoint_id}",
            output_payload=f"could not re-check the repository before rollback: {e}",
        )
        raise CheckpointError(
            f"Could not verify the repository state before rolling back ({e}). Nothing was changed. "
            f"Run `jarvis rollback --checkpoint {checkpoint_id}` again."
        ) from e
    if unchanged:
        return
    tracker.log_execution(
        cp_task_id, "git_rollback", "SAFE_WRITE", "FAILURE",
        input_payload=f"checkpoint={checkpoint_id}",
        output_payload="repository changed while waiting for confirmation; nothing was changed",
    )
    raise CheckpointError(
        "The repository changed while you were deciding (a file was edited, or a commit or "
        "branch change happened), so the preview you confirmed is out of date. Nothing was "
        f"changed. Run `jarvis rollback --checkpoint {checkpoint_id}` again to see the new preview."
    )


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
    Takes the run lock of every plan that used this checkpoint, then rolls back (see
    _rollback_locked). A plan that is running right now refuses the rollback instead of having
    its work erased under it, and a run cannot start while the rollback is waiting at [y/N]
    (Antigravity's review of M002.1, finding 2).
    """
    cp = get_checkpoint(tracker.db_path, checkpoint_id)
    if cp is None:
        raise CheckpointError(f"No checkpoint found with id '{checkpoint_id}'.")
    store = PlanStore(tracker.db_path)
    held: list[tuple[str, str]] = []
    try:
        for plan in store.list_for_task(cp["task_id"]):
            if plan.checkpoint_id != checkpoint_id:
                continue
            try:
                held.append((plan.plan_id, store.acquire_run_lock(plan.plan_id)))
            except PlanLocked as e:
                raise CheckpointError(
                    f"Cannot roll back: plan {plan.plan_id} is running right now ({e}). "
                    "Let it finish or stop it, then run the rollback again."
                ) from e
        return _rollback_locked(tracker, checkpoint_id, auto_confirm=auto_confirm, out=out)
    finally:
        for plan_id, token in held:
            store.release_run_lock(plan_id, token)


def _rollback_locked(tracker, checkpoint_id: str, *, auto_confirm: bool | None = None, out=print) -> str:
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

    snapshot = _rollback_snapshot(target, commit)
    dropped, changes, untracked, deleted = snapshot[:4]

    store = PlanStore(tracker.db_path)
    linked = [p for p in store.list_for_task(cp["task_id"]) if p.checkpoint_id == checkpoint_id]

    if not dropped and not changes and snapshot[4] == commit:
        # (HEAD must BE the checkpoint: a HEAD rewound to an ancestor while the index and working
        # tree still match it has no commits to drop and no changes, yet still needs moving; F5.)
        # A plan that only created untracked files before failing leaves nothing for git to
        # undo, but it is still BLOCKED. Rolling back must let it be re-run from the start,
        # and must say plainly that the files it created are still on disk.
        stuck = [p for p in linked if p.status is not PlanStatus.PENDING]
        if not stuck:
            return f"Nothing to roll back: {project_root} already matches checkpoint {checkpoint_id}."
        out(f"\n--- Proposed rollback of {target} to checkpoint {checkpoint_id} ({commit[:10]}) ---")
        out("No tracked files differ from the checkpoint, so git has nothing to reset.")
        if untracked:
            out("Untracked files (NOT touched, still on disk, listed for your information):")
            out(untracked)
        out(f"Plan(s) that will be reset to PENDING: {', '.join(p.plan_id for p in stuck)}")
        out("---")
        confirmed = auto_confirm if auto_confirm is not None else _confirm(
            "Reset the plan state above? No files will be changed."
        )
        if not confirmed:
            tracker.log_execution(
                cp["task_id"], "git_rollback", "SAFE_WRITE", "REJECTED_BY_POLICY",
                input_payload=f"checkpoint={checkpoint_id}", output_payload="declined by user",
            )
            return "Rollback declined. Nothing was changed."
        _refuse_if_changed(tracker, cp["task_id"], checkpoint_id, target, commit, snapshot)
        for plan in stuck:
            plan.reset_for_rerun()
            store.save(plan)
        tracker.log_execution(
            cp["task_id"], "git_rollback", "SAFE_WRITE", "SUCCESS",
            input_payload=f"checkpoint={checkpoint_id}", output_payload="plan state reset; no git changes",
        )
        msg = (f"No tracked files to roll back in {project_root}. Plan(s) reset to PENDING: "
               f"{', '.join(p.plan_id for p in stuck)}.")
        if untracked:
            msg += " Untracked files the plan created are still on disk."
        return msg

    out(f"\n--- Proposed rollback of {target} to checkpoint {checkpoint_id} ({commit[:10]}) ---")
    if dropped:
        out("Commits that will be removed from the branch (still recoverable via git reflog):")
        out(dropped)
    if snapshot[4] != commit and not dropped:
        out(f"HEAD is at {snapshot[4][:10]}, not at the checkpoint; it will be moved to {commit[:10]}.")
    if deleted:
        out("Files that will be DELETED from disk (they did not exist at the checkpoint):")
        out(deleted)
    if changes:
        out("Changes to tracked files that will be DISCARDED (summary):")
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

    # An edit made in another window during the prompt would otherwise be destroyed without ever
    # having been listed. Compare again immediately before the destructive step.
    _refuse_if_changed(tracker, cp["task_id"], checkpoint_id, target, commit, snapshot)

    try:
        _git(target, "reset", "--hard", commit)
    except CheckpointError as e:
        tracker.log_execution(
            cp["task_id"], "git_rollback", "SAFE_WRITE", "FAILURE",
            input_payload=f"checkpoint={checkpoint_id}", output_payload=str(e),
        )
        raise

    reset_ids = []
    for plan in linked:
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
