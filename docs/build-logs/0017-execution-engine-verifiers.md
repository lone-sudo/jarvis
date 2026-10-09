# 0017 — M002 Execution Engine: Stream B Verifiers (Antigravity)

**Date:** 2026-10-09
**Contributors:** Antigravity (Stream B: `core/verifier.py`, verifiers, test and mutation harness).
**Branch:** `feature/antigravity-m002-verifier` (branched from `origin/feature/claude-m002-engine`)
**Status:** All 331 tests passing (15 new verifier tests + 316 existing passing, 2 symlink skips on Windows).

## What was built
| File | Role |
|---|---|
| `src/jarvis/core/verifier.py` | `FileContentVerifier`, `PytestTargetVerifier`, `GitCommitVerifier`, and `VERIFIERS` dictionary |
| `tests/test_verifiers.py` | Unit, integration, mutation, policy-breach, timeout, and diagnostic-preservation tests |

## Implementation Details

Following the contract in `ADR-0005` ("Verifier interface"):
1. **Types Imported From `core/plan.py`:**
   Imported `ExecutionStep`, `VerificationContext`, and `VerificationResult` directly; zero type redefinition.
2. **`FileContentVerifier`:**
   - Validates spec dictionary and rejects unknown keys.
   - Enforces workspace and project containment; rejects absolute paths and `..` path traversal with `PermissionError`.
   - Supports exact `content` matching, substring `contains` matching, `not_empty` checks, and automatic fallback to `step.params["content"]`.
   - Strictly read-only; never mutates target files.
3. **`PytestTargetVerifier`:**
   - Runs `[sys.executable, "-m", "pytest"]` within `ctx.project_root` to guarantee environment and venv isolation.
   - Enforces `ctx.time_remaining` as the wall-clock timeout budget; catches `subprocess.TimeoutExpired` cleanly.
   - Enforces path containment on `target`.
   - On test failure, preserves full diagnostics: exit code, assertion failure trace, and stdout/stderr output.
4. **`GitCommitVerifier`:**
   - Inspects the latest commit hash and subject using `GitInspector._run(["log", "-n1", "--format=%H%x00%s"], ctx.project_root)`.
   - Supports exact `message` matching, `message_contains` substring checks, and step param message fallback.
   - Supports `expect_clean=True` to verify that the working tree has no uncommitted changes after a commit.
5. **Auto-Discovery Registry:**
   Exposes module-level `VERIFIERS` dictionary (`"file_content"`, `"pytest_target"`, `"git_commit"`), which `ExecutionEngine.load_default_verifiers()` auto-discovers upon import.

## Results
- Full suite: **331 passed, 2 skipped** (symlinks on Windows without admin/dev-mode).
- Baseline Stream A tests (318 tests) pass with **zero regressions**.
- 15 new tests in `tests/test_verifiers.py`:
  - `test_file_content_verifier_happy_path` (PASSED)
  - `test_file_content_verifier_failures_and_diagnostics` (PASSED)
  - `test_file_content_verifier_policy_breach` (PASSED)
  - `test_file_content_verifier_spec_validation` (PASSED)
  - `test_pytest_target_verifier_happy_path` (PASSED)
  - `test_pytest_target_verifier_failure_diagnostics` (PASSED)
  - `test_pytest_target_verifier_timeout` (PASSED)
  - `test_pytest_target_verifier_policy_breach` (PASSED)
  - `test_git_commit_verifier_happy_path` (PASSED)
  - `test_git_commit_verifier_failure_diagnostics` (PASSED)
  - `test_git_commit_verifier_non_git_repo` (PASSED)
  - `test_engine_executes_plan_with_real_verifiers` (PASSED)
  - `test_engine_preserves_diagnostics_on_verifier_failure` (PASSED)
  - `test_mutation_altered_file_is_detected` (PASSED)
  - `test_mutation_verifier_does_not_mutate_project` (PASSED)

## Verification Highlights
- **Policy Breach:** Path traversal attempts (`../../outside.txt` or absolute paths) in verifier specs or parameters are blocked and raise `PermissionError`.
- **Diagnostic Preservation:** When `pytest_target` fails, the plan transitions to `BLOCKED`, the failed code remains on disk for human review, and the full pytest failure trace (`MATH REGRESSION`) is saved in the SQLite `executions` table under `verify:pytest_target`.
- **Timeout Protection:** Exhausted time remaining budget (`time_remaining <= 0`) or slow tests exceeding the timeout budget fail gracefully and return actionable timeout diagnostics.
- **Read-Only Invariant:** Verified via mtime checks that running a verifier never alters file modification timestamps or project contents.
