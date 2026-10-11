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

from pathlib import Path, PurePosixPath

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


def _parse_status_v2(output: str) -> list[tuple[str, str]]:
    """
    Parses `git status --porcelain=v2` into (two-letter code, path) pairs.

    Why v2 and not `--short`: GitInspector._run() strips its output, which removes
    the leading space of the FIRST line (" M file" becomes "M file"). Slicing that
    line at a fixed column then mangles the file name and the staged/unstaged code.
    v2 lines always start with a digit, '?' or 'u', so nothing is lost. A leading
    space can never be stripped. (Found in review of commit 0a2b4bc.)
    """
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        kind = line[:1]
        if kind == "?":
            entries.append(("??", line[2:].strip('"')))
        elif kind == "1":
            parts = line.split(" ", 8)
            if len(parts) == 9:
                entries.append((parts[1].replace(".", " "), parts[8].strip('"')))
        elif kind == "2":
            parts = line.split(" ", 9)
            if len(parts) == 10:
                entries.append((parts[1].replace(".", " "), parts[9].split("\t")[0].strip('"')))
        elif kind == "u":
            parts = line.split(" ", 10)
            if len(parts) == 11:
                entries.append((parts[1].replace(".", " "), parts[10].strip('"')))
    return entries


def _new_file_preview(project_root: Path, rel: str) -> str:
    """
    `git diff` shows nothing for a file git does not track yet, so a brand-new file would
    reach the [y/N] prompt with no content shown. Build a preview from the file itself.
    Symlinks are not followed (a preview must never read outside the project).
    """
    path = project_root / rel
    header = f"--- /dev/null\n+++ b/{rel}"
    if path.is_symlink() or not path.is_file():
        return f"{header}\n(new file; content not shown)"
    try:
        data = path.read_bytes()[: MAX_DIFF_PREVIEW + 1]
    except OSError as e:
        return f"{header}\n(new file; could not read it for preview: {e})"
    if b"\0" in data:
        return f"{header}\n(new binary file; content not shown)"
    truncated = len(data) > MAX_DIFF_PREVIEW
    text = data[:MAX_DIFF_PREVIEW].decode("utf-8", errors="replace")
    body = "\n".join("+" + line for line in text.splitlines())
    return f"{header}\n@@ new file @@\n{body}" + ("\n... [file preview truncated]" if truncated else "")


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
    # core.quotepath=off keeps non-ASCII names readable; -uall lists untracked files one by
    # one instead of collapsing a new directory to "dir/" (which made a file this plan wrote
    # appear under "NOT part of this commit").
    status_args = ["-c", "core.quotepath=off", "status", "--porcelain=v2", "--untracked-files=all"]
    status = GitInspector._run([*status_args, "--", *specs], target)
    if status.startswith("ERROR"):
        return status
    if not status:
        return "ERROR: Nothing to commit: the files this plan wrote are unchanged (or git-ignored): " + ", ".join(paths)
    all_status = GitInspector._run(status_args, target)
    if all_status.startswith("ERROR"):
        return all_status

    # What gets committed is exactly the plan's own paths (the pathspec below), so that is
    # also what the preview must call "included", never something re-derived from output text.
    included = set(paths)
    plan_entries = [(code, p) for code, p in _parse_status_v2(status) if p in included]
    excluded_entries = [(code, p) for code, p in _parse_status_v2(all_status) if p not in included]

    diff = GitInspector._run(["diff", "--", *specs], target)
    if diff.startswith("ERROR"):
        return diff
    new_files = [p for code, p in plan_entries if code == "??"]
    previews = [_new_file_preview(Path(target), p) for p in new_files]
    if previews:
        diff = "\n".join(part for part in [diff, *previews] if part)

    print(f"\n--- Proposed commit in {target} ---")
    print(f"Message: {message}")
    print("\nFiles this plan wrote, which WILL be committed (and nothing else):")
    print("\n".join(f"{code} {p}" for code, p in plan_entries))
    if diff:
        print("\nDiff:")
        print(diff[:MAX_DIFF_PREVIEW] + ("\n... [diff truncated]" if len(diff) > MAX_DIFF_PREVIEW else ""))
    if excluded_entries:
        print("\nOther files in the project, NOT part of this commit:")
        print("\n".join(f"{code} {p}" for code, p in excluded_entries[:MAX_EXCLUDED_LISTED]))
        if len(excluded_entries) > MAX_EXCLUDED_LISTED:
            print(f"... and {len(excluded_entries) - MAX_EXCLUDED_LISTED} more")
    print("---")

    confirmed = auto_confirm if auto_confirm is not None else _confirm("Commit these files?")
    if not confirmed:
        return "ERROR: Access denied by policy. (declined by user)"

    add_result = GitInspector._run(["add", "--", *specs], target)
    if add_result.startswith("ERROR"):
        return add_result
    # The pathspec form commits only these paths, even if something else is already staged.
    return GitInspector._run(["commit", "-m", message, "--", *specs], target)
