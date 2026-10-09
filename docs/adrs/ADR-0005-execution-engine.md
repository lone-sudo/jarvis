# ADR-0005: Execution Engine — Per-Step Authorization, Targeted Verification, Evidence-Preserving Failure

**Status:** Proposed — sandbox-tested only. Not accepted until Lone has verified it on the real ZBook
(invariant 5: sandbox-verified ≠ real-machine-verified).
**Date:** 2026-10-09

## Context
Milestone 001 gave Jarvis state, memory, and individually-confirmed tools. M002 adds the thing that
strings tools together: a deterministic substrate that takes a short, bounded, human-readable plan
and executes it step by step, so work can be handed to a model-written plan without handing over
the keys. The engine is a replaceable-reasoning-backend boundary: models propose plans, the engine
decides what actually runs.

    Execution Plan -> Execution Engine -> Per-Step Policy Check -> Tool Invocation
                   -> Targeted Verification -> Durable State

## Decisions (agreed across the team)
1. **Per-step re-authorization.** Approving a plan never grants a bypass. Each step is authorized at
   execution time (tool registered? tier allowed? path via `resolve_safe_path()`), and `write_file` /
   `git_commit` still show a diff and ask `[y/N]` per step. Implemented in `ExecutionEngine._authorize_step`,
   tested independently of the tools' own checks.
2. **Configurable boundaries.** `max_steps` (default 5) and `timeout_seconds` (default 120), set per plan.
3. **Targeted verification, not generic cleanliness.** A step is verified against its own intended
   postcondition. `GitCleanVerifier` is not a default (edits dirty the tree by design).
4. **Diagnostic preservation.** On failure the engine never deletes or reverts. Error, tool output,
   verifier stdout/stderr, traceback, `git status` and `git diff` are saved to `executions`
   (`tool_name='engine:failure'`), shown in the terminal, and the plan and task go `BLOCKED`.
5. **No stash/branch bloat.** The dirty tree stays on disk. A pre-flight checkpoint (a row in the existing
   `checkpoints` table: branch + HEAD hash) lets the human run `jarvis rollback --checkpoint <id>`.
6. **Minimal persistence.** `execution_plans` (migration 005): `plan_id, task_id, status,
   current_step_index, max_steps, plan_json, created_at, updated_at`. `plan_json` is canonical; the
   other columns are same-statement copies for filtering. `timeout_seconds` and `checkpoint_id` live in the JSON.

## Implementation decisions made while building — **please confirm or reverse**
These go beyond the six above. Each is a deliberate choice with a reason, not an accident.

| # | Decision | Why | Cost |
|---|---|---|---|
| a | **Every mutating step must declare a `verify` spec**, and every verifier a plan names must be installed, checked in pre-flight before *any* step runs. | Otherwise a write can execute and then turn out unverifiable. | Write plans cannot run until `core/verifier.py` is merged. Read-only plans run today. |
| b | **Mutating steps are confined to the task's project directory**, stricter than workspace containment. | A checkpoint only protects the project's own repo. A write into a sibling project (still "inside the workspace") would be something `rollback` cannot undo. Found by a failing test, not by design. | A plan cannot touch two projects. Reads are still workspace-wide. |
| c | **A plan with mutating steps needs a rollback point first:** project must be a git repo with ≥1 commit and **no uncommitted changes to tracked files** (untracked files are fine). Otherwise the plan is `BLOCKED` in pre-flight with nothing run. | `git reset --hard <hash>` is only a faithful "undo this plan" if nothing else was uncommitted at the start. | You must commit or stash WIP before running a write plan. |
| d | **`timeout_seconds` is a per-run wall-clock budget, checked before each step.** Time spent at `[y/N]` prompts counts. | An in-process tool call cannot be safely killed, and the prompt is inside the tool. | **120s default is short if you read diffs carefully. Raise `--timeout` for write plans.** A timeout leaves the step un-started (still `PENDING`) and the plan resumable. |
| e | **The engine never marks a task `DONE`.** A completed plan leaves the task `IN_PROGRESS`. | `DONE` is terminal and a task may have several plans. | There is currently no CLI command to close a task. Needs a decision. |
| f | **Re-running a `BLOCKED` (or interrupted `RUNNING`) plan resumes at the failed step.** The original checkpoint is kept. Rolling back resets any plan that used that checkpoint to `PENDING` step 0. | Otherwise a plan could resume past work a rollback had erased. | — |
| g | **Plan tools are exactly `read_file`, `list_directory`, `write_file`, `git_commit`.** Anything else is denied by default. No network steps. | Keeps M002 inside the non-goals and the $0 policy. | — |

## Verifier interface (contract for `src/jarvis/core/verifier.py`, Antigravity's stream)
The three types below already exist in `core/plan.py`. Import them; do not redefine.

```python
from jarvis.core.plan import ExecutionStep, VerificationContext, VerificationResult

# VerificationResult(ok: bool, detail: str = "")      detail = the evidence: pytest stdout/stderr, mismatch, commit hash...
# VerificationContext(project_root: Path,             absolute
#                     workspace_root: Path,           absolute
#                     time_remaining: float)          seconds left in the plan's budget, >= 0: use as subprocess timeout

class FileContentVerifier:
    def __init__(self, spec: dict): ...                       # may raise -> plan BLOCKED in pre-flight, nothing run
    def verify(self, step: ExecutionStep, ctx: VerificationContext) -> VerificationResult: ...

# Module-level registry the engine auto-discovers (absent module = no verifiers, which is fine):
VERIFIERS = {
    "file_content": FileContentVerifier,       # step.verify == {"type": "file_content", ...verifier's own keys}
    "pytest_target": PytestTargetVerifier,
    "git_commit": GitCommitVerifier,
}
```
Rules: `verify()` must not mutate project files. A returned `ok=False` is a normal verification failure
(plan `BLOCKED`, evidence kept). An *exception* is recorded separately as `VERIFIER_ERROR` with traceback.
If this contract doesn't suit what is already written, say so and the engine adapts, rather than
the reverse being assumed.

## Consequences and known limitations
- Tools report errors as strings starting `ERROR:`. The engine classifies on that prefix (and the
  `(declined by user)` marker). A `read_file` whose *content* begins with `ERROR:` would be misread as a
  failure. Fixing it means changing the tool return protocol, which is out of scope here.
- `tools/base.py::run_logged` labels any result containing "denied by policy" as `REJECTED_BY_POLICY`,
  even a successful `read_file` of a document that merely quotes that phrase (these ADRs do). The engine
  therefore logs its own step rows with an exact status instead of using `run_logged`. `run_logged` itself is unchanged.
- `git reset --hard` drops commits from the branch (recoverable via `git reflog`) and discards tracked
  changes. Rollback previews both, refuses on a different branch, and asks `[y/N]`.
- No locking: two simultaneous `plan-run` processes on one plan are not guarded against.
- The engine relies on `GitInspector._run` / `_authorized_target` (module-private helpers) and adds
  `core/checkpoint.py`; `tools/git.py` itself is untouched.
- Diagnostics: `git diff` is capped at 50,000 characters, other text fields at 20,000.

## Non-goals (unchanged)
No autonomous loops, no model-written steps executed without per-step confirmation, no network steps,
no multi-machine execution (M003), no automatic rollback.
