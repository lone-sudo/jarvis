# 0002 — V1: Confirmed Writes + Provider State Hooks

**Date:** 2026-09-20
**Contributors:** Lone, ChatGPT, Claude, Gemini

## What happened
Implemented Gemini's two V1 requirements from the M001 review:

1. **Explicit write confirmations.** `FileSystemInspector.write_file` and
   `GitInspector.commit_changes` are now SAFE_WRITE-tier tools that print a diff
   (unified diff for file writes; `git status --short` + `git diff` for commits)
   and require an explicit `[y/N]` terminal confirmation before touching disk.
   Declining is not a silent no-op — it returns an "Access denied by policy
   (declined by user)" result, which the new `tools/base.py::run_logged` wrapper
   recognizes and logs as `REJECTED_BY_POLICY` in the `executions` table, not
   `FAILURE` or `SUCCESS`.
2. **Airtight state transitions around the provider adapter.**
   `ManualClipboardProvider.dispatch_and_track()` sets the task to `AWAITING_USER`
   *before* the clipboard is populated / prompt is printed — not after — and only
   moves it to `IN_PROGRESS` or `DONE` once Lone has explicitly pasted a response
   back into the CLI. `DONE` is only set when the caller explicitly passes
   `mark_done=True`; it is never inferred from the response text itself.

## New CLI commands
- `jarvis write-file --task TSK-... <path> --content "..."` (or `--content-file`)
- `jarvis git-commit --task TSK-... --project <key> --message "..."`
- `jarvis ask-ai --task TSK-... [--provider Claude_Web] "<prompt>" [--done]`

## What was verified, not just implemented
- Real interactive run (not mocked): attempted a file write, declined it — file was
  not created, execution logged as `REJECTED_BY_POLICY`. Ran it again, confirmed it —
  file was created, execution logged as `SUCCESS`. Same pattern for `git-commit`:
  declined leaves the repo dirty and uncommitted; confirmed produces a real commit
  (verified via `git log`).
- Automated test specifically proves the task is at `AWAITING_USER` *during* the
  provider call, not just before/after it — by having the mocked `dispatch_prompt`
  read the task's own status mid-call.
- Automated test for the crash case Gemini's requirement implies but doesn't state
  outright: if the process is interrupted while waiting on the human (terminal
  closed before pasting a response), the task is left at `AWAITING_USER`, not reset
  to anything else — this is what makes `jarvis resume` trustworthy after a real
  crash, not just a clean exit.
- 21 tests pass total (13 from M001 + 8 new).

## Next (V2 candidates, not started)
- Content inbox (manual save, tag, no auto-task creation)
- Keyword-based dedup/clustering across saved content
- Nightly consolidation into project Markdown summaries (archive raw, never delete)
