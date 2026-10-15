# 0021: task-done with confirmation (M002.1, item 2)

## Problem
Nothing closed a task except `ask-ai --done`, which marked it DONE with no confirmation. DONE is terminal
(no transition out of it, and the engine refuses to run any plan against a DONE task). Build log 0019 task 2
found that all four candidate implementations closed a task without a `[y/N]`, because invariant 3 named
only `write-file` and `git-commit`. Adding `task-done` alone would have left `ask-ai --done` as an
unconfirmed route to the same irreversible change.

## Change
- New `core/task_completion.py::complete_task`, the only place that sets DONE from the CLI. It shows the task,
  its plans and a warning for any plan not COMPLETED, then asks `[y/N]` (default no).
  Refused before any prompt: unknown task, and PENDING, BLOCKED or ABORTED tasks (the state machine does not
  allow DONE from them). Already DONE is reported and nothing changes, so repeating it is harmless.
- New command `jarvis task-done TASK_ID`, logged as `task_done` (SAFE_WRITE). A "no" is logged as REJECTED_BY_POLICY.
- `ask-ai --done` no longer passes `mark_done=True`. It dispatches, shows the response, then asks the same
  question. Declining leaves the task IN_PROGRESS. The response is shown first so the decision is informed.
- `AGENTS.md` invariant 3 and ADR-0005 (e) updated. The engine still never closes a task by itself.
- Not exposed on Telegram (read-only commands only, enforced by an existing test).

## Verification (Linux sandbox only)
- New `tests/test_task_done.py`: 22 tests, including the CLI paths and a check that a COMPLETED plan leaves its task IN_PROGRESS.
- Mutation checks: restoring the old `mark_done=args.done` failed the two `ask-ai --done` tests. Removing
  the confirmation failed six. Both restored.
- Full suite: 488 passed, 0 failed, 0 skipped on Linux (466 on main + 22 new).

## Not verified
- **Not run on the ZBook.** Prompts use `input()`, as the existing confirmed commands do.
- Reviewed by no other model yet.
- `--done` on `ask-ai` now means "offer to mark DONE", not "mark DONE". Anyone scripting it non-interactively
  will see it stop at the prompt. Nothing in the repo does.
