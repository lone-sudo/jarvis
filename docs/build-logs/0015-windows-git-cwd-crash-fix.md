# 0015 — Fix: Windows Crash on Missing Project Directory (`NotADirectoryError`)

**Date:** 2026-09-29
**Contributors:** Lone (caught it live on Windows, with help from Gemini while Claude was at capacity), Claude (diagnosed root cause + fixed)

## Context
Lone hit a real crash testing the Telegram interface for real, plus a separate test-environment
issue, both worth recording honestly. Some diagnosis/workaround steps were done with Gemini's
help while Claude was unavailable — documented here per standing practice, reconciled against
the actual repo state rather than taken on narrative alone (an earlier round found a described
fix, `gemini-3.8-flash`, didn't match what was actually on disk — turned out to be a real,
verified-correct model name, but the file state needed checking directly regardless).

## Issue 1: wrong Python environment running pytest (environmental, not a code bug)
`pytest` was resolving to a global Python install instead of the project's `.venv`, causing
`ModuleNotFoundError` for `pyperclip` across several files. Fixed on Lone's end by installing
`pytest` into the venv and invoking it via the venv's own `pytest.exe` — confirmed by the second
run's platform line showing `...\.venv\Scripts\python.exe` instead of the AppData global path.
No code change needed for this one.

## Issue 2: `test_backend_selection.py`'s fragile pyperclip mock (a real gap I'd already fixed elsewhere and missed here)
Build-log 0010 already diagnosed and fixed this exact pattern in `test_provider_and_schema_fix.py`:
`unittest.mock.patch("jarvis.providers.base.pyperclip")` fails with `AttributeError` if
`pyperclip` didn't bind as a module attribute in that specific process (environment-dependent,
confirmed to vary even on the same machine). I applied that fix to one file at the time and
missed that `test_backend_selection.py` (written later, for the Gemini-provider round) had the
same unguarded pattern. Fixed now with `create=True`.

## Issue 3: the real bug — `NotADirectoryError` crashing `/resume` on Windows
**Root cause, confirmed by reading the actual code, not assumed:** `GitInspector.get_status()`
checks `target_dir.exists()` before running git; `get_current_branch()` and `get_recent_log()`
never had that check — they went straight to `subprocess.run(cwd=target_dir)`. On POSIX, a
missing `cwd` raises `FileNotFoundError`, which `_run()` already caught (if with a slightly
misleading "git executable not found" message). **On Windows, the same situation raises
`NotADirectoryError` instead** — a real platform difference, not a logic error — which nothing
caught. `cmd_resume` calls `get_current_branch()` *before* `get_status()`, so it was the first
to hit this for a registered project whose directory doesn't exist on disk (a realistic case:
register the project before cloning it, or after the folder's been moved/deleted).

## Fixed
- Consolidated all three read methods (`get_status`, `get_current_branch`, `get_recent_log`)
  plus `commit_changes` through one shared `_authorized_target()` pre-flight check (policy →
  existence → `.git` presence), so this exact inconsistency — one method checked, two didn't —
  can't recur silently.
- `_run()` now also catches `NotADirectoryError` explicitly as defense in depth, in case any
  future caller reaches it without going through the shared pre-flight check.
- `GitInspector` had **no dedicated test file before this** — only indirect coverage through
  `cmd_resume`/session-consolidation tests, none of which exercised a missing directory. Added
  `tests/test_git_inspector.py`: all three read methods tested directly against a missing
  directory, a non-git directory, a real repo (branch/log/status clean-vs-dirty), and path
  traversal — closing a real coverage gap, not just patching the one symptom that surfaced.
- The Telegram test that caught this now has two versions: the original, fixed to set up a real
  git repo (the realistic common case), and a new dedicated regression test that deliberately
  does NOT create the directory, proving `/resume` degrades gracefully (shows the task, reports
  git context as unavailable) instead of crashing the whole reply.

## Verified
- 61 tests (git inspector + telegram + backend selection) pass together.
- Full suite: 217 tests (206 previous + 11 new), all passing.
- The exact failure mode from Lone's run — a registered project with no directory on disk,
  reached through the Telegram `/resume` path — is now a passing regression test, not a crash.

## Honest note on this round's process
Diagnosis and a workaround attempt happened via Gemini while Claude was at capacity, which is a
reasonable use of the team setup. The lesson worth keeping: reconcile against the actual current
file contents before trusting a fix narrative, even when the narrative turns out to be accurate
(as the model-name fix was) — `git status`/`git diff` settled in minutes what a description alone
couldn't.
