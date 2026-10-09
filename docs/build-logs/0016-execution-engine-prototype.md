# 0016 — M002 Execution Engine: Stream A prototype (Claude)

**Date:** 2026-10-09
**Contributors:** Claude (Stream A: plan model, store, engine, checkpoint/rollback, CLI). Antigravity owns Stream B (`verifier.py`, integration and mutation harness) and was not involved in this log.
**Branch:** `feature/claude-m002-engine` (cut from `main` at `6b3a6a9`)
**Status:** Sandbox-verified only. **Not verified on the real ZBook.**

## What was built
| File | Role |
|---|---|
| `src/jarvis/core/plan.py` | `ExecutionPlan`, `ExecutionStep`, `StepStatus`, `PlanStatus`, strict JSON (de)serialization, verifier contract types |
| `src/jarvis/state/migrations/005_execution_plans.sql` | `execution_plans` table |
| `src/jarvis/state/plan_store.py` | persistence, failure-diagnostics lookup |
| `src/jarvis/core/engine.py` | `ExecutionEngine`: pre-flight, per-step authorization, boundaries, verification hook, `_preserve_failure`, resume |
| `src/jarvis/core/checkpoint.py` *(new, not in the original file list)* | pre-flight checkpoint + confirmed rollback |
| `src/jarvis/interface/cli.py` | `plan-create`, `plan-run`, `plan-show`, `rollback` |
| `docs/adrs/ADR-0005-execution-engine.md` | decisions, the verifier contract, and the choices needing confirmation |

`core/checkpoint.py` is a separate file so that nothing in `tools/git.py` (shared, outside both streams' lists) had to change.

## Results
- Baseline: **217 passed** before any change, on a fresh shallow clone of `main`.
- After: **317 passed** (217 baseline + 100 new): plan model/store, checkpoint/rollback against real temporary git repos, the engine, and the CLI end to end.
- Mutation spot-check, run on a scratch copy: removing per-step authorization, ignoring the verifier verdict, marking failed steps `PENDING`, and dropping the rollback-point requirement each made tests fail (3, 5, 5, 2 failures). So the tests guard those behaviours rather than merely exercising them.
- Existing tests needed **no changes**. `test_migrations.py` reads the latest migration number dynamically, so adding 005 did not disturb it.

## Findings worth recording
1. **A write could escape its project while staying inside the workspace.** My first test asserted `../other/escape.txt` would be denied. It wasn't: that path is inside the workspace, so workspace policy allows it. The consequence is more than a policy gap, because a checkpoint only covers the task's own repo, so such a write could never be rolled back. Mutating steps are now confined to the project (ADR-0005 b). Caught by a failing test, which is the process working.
2. **`run_logged` mislabels successful reads.** It treats any result containing "denied by policy" as `REJECTED_BY_POLICY`. Our own ADRs contain that phrase, so a plan step reading them would be logged as a rejection. Not changed here; the engine logs its own exact status instead. Worth a separate fix.
3. **The 120s default `timeout_seconds` includes human think time at `[y/N]` prompts.** Fine for read-only plans; likely too short for a write plan where you read diffs. Documented, and `--timeout` overrides it.

## Not done / not verified
- **Nothing here has run on Windows 11.** Path handling goes through the existing `resolve_safe_path` and `Path(...).as_posix()`, and all printed output is plain ASCII, but that is reasoning, not evidence.
- `core/verifier.py` does not exist on this branch. Until Stream B merges, write plans stop in pre-flight with a clear message ("no verifier is installed for type ..."), and only read-only plans run end to end. The engine picks up `VERIFIERS` from that module automatically once present; the contract is in ADR-0005.
- No concurrency guard between two simultaneous `plan-run` processes.
- No `task-done` command: a completed plan leaves its task `IN_PROGRESS` (ADR-0005 e).
- Nothing pushed. The session's GitHub access to `lone-sudo/jarvis` is read-only (the push check was refused), so the branch exists locally in the sandbox only.
