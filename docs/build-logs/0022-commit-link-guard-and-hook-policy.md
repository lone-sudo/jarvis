# 0022: junction guard on the plan commit, and the hook policy note (M002.1, items 3 and 4)

## Junction guard
The rollback refuses a tracked folder that has become a link or junction (build log 0016, digest review 2),
but the plan `git_commit` path (`core/plan_commit.py`) had no equivalent. Git for Windows walks a junction
as an ordinary folder, and `Path.is_symlink()` is False for one, so a folder swapped for a junction after
the plan wrote into it could make the commit stage a file from outside the project, and the preview would
show that outside content as the plan's own.

- `_first_link_on_the_way_to` checks every parent folder of every plan path inside the project with the
  rollback's own `_is_link`, which treats a junction like a symlink. The file itself is not checked: my first
  version did check it, and the existing test `test_preview_does_not_follow_a_symlink_for_new_file_content`
  failed, because git records a symlinked file as a link and the preview already handles it safely.
- It runs twice: before anything is shown, and again after the `[y/N]` and before `git add`, because the
  prompt can stay open indefinitely. A refusal changes nothing and returns an `ERROR:` string, so the plan
  goes BLOCKED with the message in its diagnostics, as for any tool error.
- Deliberately narrower than the rollback guard: it looks only at the plan's own paths, not every tracked folder.

## Hook policy
Recorded in ADR-0005, as Lone decided: repository hooks are already off for every git command except
`commit`, a plan's commit still runs the project's own hooks, and that is documented, not blocked.
No code change. The test that pins the behaviour already existed.

## Verification (Linux sandbox only)
- New `tests/test_plan_commit_link_guard.py`: 7 tests, 6 run on Linux. A simulated junction (patching
  `Path.is_junction`, the technique the rollback tests use), a link at the file itself is not refused by this
  guard, a real symlink folder pointing outside (nothing leaks into the repo), a link swapped in during the
  prompt, a decline still reads as a decline, and an ordinary commit still works. The 7th, a real Windows junction made with `mklink /J`, skips on Linux.
- Mutation checks: removing the pre-preview check failed 2 tests, removing the post-prompt check failed the
  swap test. Both restored.

## Not verified
- **The real junction test has not run anywhere.** It needs the ZBook.
- Not reviewed by another model yet.
- Reading status or a diff through a junction in a tracked folder (not a plan path) is still not blocked on
  the commit path. Only rollback refuses that.
- **Python floor.** `pyproject.toml` declares Python 3.10 or later, but `Path.is_junction` only exists from 3.12.
  The shared `_is_link` in `core/checkpoint.py` falls back to "not a junction" without it, so on 3.10 or 3.11 neither
  this guard nor the rollback guard can see a Windows junction (a symlink is still seen). The ZBook runs 3.14, so it
  is covered there. Not fixed here: the shared helper belongs in its own change. The fix in words: on Windows, detect
  a junction from `os.lstat` (the reparse-point attribute plus the mount-point reparse tag) when `is_junction` is missing.
