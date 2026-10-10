"""
The plan-scoped commit used by a plan's `git_commit` step (M002, ADR-0005 h).

Why this exists instead of GitInspector.commit_changes: that tool runs
`git add -A`, which commits *every* untracked file in the project. Inside
a plan that is dangerous. Those files were never part of the plan, yet
the commit makes them tracked, and a later `jarvis rollback` (git reset
--hard) would then delete them from disk. Found by running a real write
plan, not by reading the code.

A plan commit therefore stages and commits only the exact paths that
earlier `write_file` steps of the same plan wrote. Nothing else is
swept in, and the preview names what is being left out.
"""

from pathlib import PurePosixPath

from jarvis.policy.rules import ActionTier
from jarvis.tools.git import GitInspector

MAX_DIFF_PREVIEW = 4000
MAX_EXCLUDED_LISTED = 20


def _confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")


def _literal(path: str) -> str:
    # ':(literal)' stops git treating *, ? or [ in a file name as a pattern,
    # which could otherwise match (and commit) files the plan never wrote.
    return f":(literal){path}"


def _clean_paths(paths: list[str]) -> list[str]:
    """Normalise to unique, project-relative, forward-slash paths. Raises ValueError on anything unsafe."""
    cleaned: list[str] = []
    for raw in paths:
        p = PurePosixPath(raw.replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts or not p.parts:
            raise ValueError(f"unsafe path '{raw}'")
        norm = p.as_posix()
        if norm not in cleaned:
            cleaned.append(norm)
    return cleaned


def commit_plan_paths(
    project_root: str, message: str, paths: list[str], auto_confirm: "bool | None" = None
) -> str:
    """
    SAFE_WRITE tier. Commits exactly `paths` (project-relative) after showing
    them and asking [y/N]. Returns git's output, or a string starting 'ERROR'
    (the tools' convention; declining contains '(declined by user)').
    """
    if not paths:
        return (
            "ERROR: Nothing to commit: a plan's git_commit only commits files written by "
            "earlier write_file steps of the same plan, and none were found."
        )
    try:
        paths = _clean_paths(paths)
    except ValueError as e:
        return f"ERROR: Refusing to commit: {e}."

    target, error = GitInspector._authorized_target(
        "git_commit", ActionTier.SAFE_WRITE, project_root, require_git_repo=True
    )
    if error:
        return error

    specs = [_literal(p) for p in paths]
    status = GitInspector._run(["status", "--short", "--", *specs], target)
    if status.startswith("ERROR"):
        return status
    if not status:
        return "ERROR: Nothing to commit: the files this plan wrote are unchanged (or git-ignored): " + ", ".join(paths)

    diff = GitInspector._run(["diff", "--", *specs], target)
    all_status = GitInspector._run(["status", "--short"], target)
    included = {line[3:].strip().strip('"') for line in status.splitlines()}
    excluded = [ln for ln in all_status.splitlines() if ln[3:].strip().strip('"') not in included]

    print(f"\n--- Proposed commit in {target} ---")
    print(f"Message: {message}")
    print("\nFiles this plan wrote, which WILL be committed (and nothing else):")
    print(status)
    if diff:
        print("\nDiff:")
        print(diff[:MAX_DIFF_PREVIEW] + ("\n... [diff truncated]" if len(diff) > MAX_DIFF_PREVIEW else ""))
    if excluded:
        print("\nOther files in the project, NOT part of this commit:")
        print("\n".join(excluded[:MAX_EXCLUDED_LISTED]))
        if len(excluded) > MAX_EXCLUDED_LISTED:
            print(f"... and {len(excluded) - MAX_EXCLUDED_LISTED} more")
    print("---")

    confirmed = auto_confirm if auto_confirm is not None else _confirm("Commit these files?")
    if not confirmed:
        return "ERROR: Access denied by policy. (declined by user)"

    add_result = GitInspector._run(["add", "--", *specs], target)
    if add_result.startswith("ERROR"):
        return add_result
    # The pathspec form commits only these paths, even if something else is already staged.
    return GitInspector._run(["commit", "-m", message, "--", *specs], target)
