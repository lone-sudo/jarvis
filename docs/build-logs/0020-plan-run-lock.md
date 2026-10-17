# 0020: plan-run concurrency guard (M002.1, item 1)

## Problem
`ExecutionEngine.run` read the plan, then later saved it as `RUNNING`, with nothing claiming it in between.
Two `plan-run` processes on one plan could both pass the read and both execute the same step (ADR-0005
listed this as "No locking"). Build log 0019 task 3 showed three of four candidate designs; this uses the
pid lock table (its A1) and avoids the localhost-socket design, which refused legitimate runs intermittently.

## Design
- Migration 006: `plan_run_locks(plan_id PRIMARY KEY, owner_pid, owner_token, acquired_at)`, cascade-deleted with the plan.
- `PlanStore.acquire_run_lock` inserts the row. The primary key makes the claim atomic across processes.
  On a clash it reads the holder: owner alive means `PlanLocked`, owner dead means compare-and-delete on the
  holder's token, then retry the insert. Of several racing takers exactly one wins.
- `ExecutionEngine.run` claims first, then loads the plan (so it never acts on state another process
  changed after a stale read), runs the unchanged body as `_run_locked`, and releases in `finally`.
  A locked plan raises `EngineError` (the CLI already prints it) with nothing changed.
- `_pid_alive` fails toward "alive". On Windows it uses `OpenProcess` and `GetExitCodeProcess` through
  `ctypes`, because `os.kill(pid, 0)` terminates the process there.
- Coordination state stays in SQLite, no lock file, so invariant 3 is not touched. `AGENTS.md` now says so.

## Verification (Linux sandbox only)
- New `tests/test_plan_run_lock.py`: 22 tests, including two real child processes (one holds the lock,
  one dies with `os._exit` without releasing) and 8 threads racing for the same plan, 10 rounds.
- Full suite: 488 passed, 0 failed, 0 skipped (466 before + 22 new). 30 consecutive runs of the new file: 0 failures.
- Mutation checks: making every owner count as dead failed 14 tests. Removing the engine's claim failed the
  two engine-level tests that need a live holder. Both restored.

## Not verified
- **Not run on the ZBook.** The Windows branch of `_pid_alive` (the `ctypes` calls) never executed here.
  Needs a real Windows run, including a child process that exits without releasing.
- A reused process id can make a stale lock look alive. The run then refuses and says which process id holds it.
  There is no unlock command yet.
- ~~`jarvis rollback` does not take the lock.~~ Fixed after Antigravity's review (finding 2, reproduced): `rollback` now takes the run lock of every plan that used the checkpoint, so a live run refuses it and no run can start while it waits at `[y/N]`. Locks are released on success, decline and error. `tests/test_rollback_run_lock.py` (7 tests; 2 fail without the fix, checked by removing it). Linux: 495 passed.
- Reviewed by no other model yet (author must not self-review).
