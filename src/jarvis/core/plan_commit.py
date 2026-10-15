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

from jarvis.core.checkpoint import _is_link
from jarvis.policy.rules import ActionTier
from jarvis.tools.git import GitInspector

MAX_DIFF_PREVIEW = 4000
MAX_EXCLUDED_LISTED = 20


def _first_link_on_the_way_to(target: Path, paths: list[str]) -> "str | None":
    """
    The first parent folder (project-relative) of any plan path that is a symlink or a Windows
    directory junction, else None. The file itself is not checked: git records a symlinked file as a
    link rather than reading through it, and the preview already refuses to show its content
    (test_preview_does_not_follow_a_symlink_for_new_file_content).

    Git for Windows treats a junction as an ordinary folder and walks into it. Staging a path under a
    junction would read a file from outside the project and commit it into the repository, and the
    preview would show that outside content as if it were the plan's. The write step is confined to the
    project, but a link can be put in place afterwards, so this is checked here too, as rollback does
    for tracked folders (`_refuse_if_tracked_folder_is_a_link`).
    """
    for rel in paths:
        parts = PurePosixPath(rel).parts
        current = Path(target)
        for depth, part in enumerate(parts[:-1], start=1):
            current = current / part
            if _is_link(current):
                return "/".join(parts[:depth])
    return None


def _link_refusal(link: str, when: str) -> str:
    return (
        f"ERROR: Refusing to commit: the folder '{link}' is a link or junction{when}. Git would read it as an "
        f"ordinary folder and could commit files from outside the project. Nothing was staged or committed. "
        f"Remove the link and restore the real folder, then re-run the plan."
    )


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
    Parses `git status --porcelain=v2 -z` into (two-letter code, path) pairs.

    Two traps, both found in review (0a2b4bc, then Antigravity's Task 003):
      * GitInspector._run() strips its output, which removes the leading space of the FIRST
        line of `--short` output (" M file" becomes "M file"), so slicing at a fixed column
        mangled the name. v2 entries always start with a digit, '?' or 'u'.
      * Without -z, git wraps any name containing a quote, backslash, tab or newline in
        C-style quotes with escapes. With -z entries are NUL-separated and paths are never
        quoted, so no unescaping is needed and nothing can be mis-parsed.
    A rename entry ('2') is followed by one extra NUL-separated token: the original path.
    """
    entries: list[tuple[str, str]] = []
    tokens = output.split("\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        kind = tok[:1]
        if kind == "?":
            entries.append(("??", tok[2:]))
        elif kind == "1":
            parts = tok.split(" ", 8)
            if len(parts) == 9:
                entries.append((parts[1].replace(".", " "), parts[8]))
        elif kind == "2":
            parts = tok.split(" ", 9)
            if len(parts) == 10:
                entries.append((parts[1].replace(".", " "), parts[9]))
            i += 1  # skip the original path that follows a rename entry
        elif kind == "u":
            parts = tok.split(" ", 10)
            if len(parts) == 11:
                entries.append((parts[1].replace(".", " "), parts[10]))
    return entries


def _new_file_preview(project_root: Path, rel: str) -> str:
    """
    `git diff` shows nothing for a file git does not track yet, so a brand-new file would
    reach the [y/N] prompt with no content shown. Build a preview from the file itself.
    The preview must never read outside the project: the file and EVERY parent directory are
    resolved first, so a symlinked parent or a Windows junction cannot lead out of it.
    """
    header = f"--- /dev/null\n+++ b/{rel}"
    path = project_root / rel
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
    except (OSError, RuntimeError, ValueError):
        return f"{header}\n(new file; content not shown: it resolves outside the project)"
    if path.is_symlink() or not resolved.is_file():
        return f"{header}\n(new file; content not shown)"
    try:
        with resolved.open("rb") as handle:       # read only what the preview shows, never the whole file
            data = handle.read(MAX_DIFF_PREVIEW + 1)
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

    link = _first_link_on_the_way_to(target, paths)
    if link:
        return _link_refusal(link, "")

    specs = [_literal(p) for p in paths]
    # -z: NUL-separated, never-quoted paths (see _parse_status_v2). -uall lists untracked files
    # one by one instead of collapsing a new directory to "dir/" (which made a file this plan
    # wrote appear under "NOT part of this commit").
    # --no-renames: a pathspec commit of only a renamed file's new name leaves the staged deletion
    # of its old name behind, so the old name must be listed on its own (Task 005, F5).
    status_args = ["status", "--porcelain=v2", "-z", "--no-renames", "--untracked-files=all"]
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

    # Against HEAD, not the index: `git commit -- <paths>` commits the working-tree content of
    # these paths, so the preview must show working tree vs HEAD. A plain `git diff` (working tree
    # vs index) showed nothing for a file that was already staged (Task 006 review, finding 2).
    has_head = not GitInspector._run(["rev-parse", "--verify", "-q", "HEAD"], target).startswith("ERROR")
    diff = GitInspector._run(["diff", *(["HEAD"] if has_head else []), "--", *specs], target)
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

    # The prompt can stay open for as long as the person likes: look again before staging anything.
    link = _first_link_on_the_way_to(target, paths)
    if link:
        return _link_refusal(link, " (it changed while the prompt was open)")

    add_result = GitInspector._run(["add", "--", *specs], target)
    if add_result.startswith("ERROR"):
        return add_result
    # The pathspec form commits only these paths, even if something else is already staged.
    return GitInspector._run(["commit", "-m", message, "--", *specs], target)
