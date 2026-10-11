# 0016 — M002 Execution Engine: Stream A prototype (Claude)

**Date:** 2026-10-09
**Contributors:** Claude (Stream A: plan model, store, engine, checkpoint/rollback, CLI). Antigravity owns Stream B (`verifier.py`, integration and mutation harness) and was not involved in this log.
**Branch:** `feature/claude-m002-engine` (cut from `main` at `6b3a6a9`)
**Status:** Verified on the real ZBook: read-only path (2026-10-09); write plans, verification failure, plan commit scope, rollback and project confinement on the merged engine + verifier branch (2026-10-10). See "Not done / not verified" for what remains.

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
- After: **330 passed** (217 baseline + 113 new, after the commit-scope fix below): plan model/store, checkpoint/rollback against real temporary git repos, the engine, and the CLI end to end.
- Mutation spot-check, run on a scratch copy: removing per-step authorization, ignoring the verifier verdict, marking failed steps `PENDING`, and dropping the rollback-point requirement each made tests fail (3, 5, 5, 2 failures). So the tests guard those behaviours rather than merely exercising them.
- Existing tests needed **no changes**. `test_migrations.py` reads the latest migration number dynamically, so adding 005 did not disturb it.

## Findings worth recording
1. **A write could escape its project while staying inside the workspace.** My first test asserted `../other/escape.txt` would be denied. It wasn't: that path is inside the workspace, so workspace policy allows it. The consequence is more than a policy gap, because a checkpoint only covers the task's own repo, so such a write could never be rolled back. Mutating steps are now confined to the project (ADR-0005 b). Caught by a failing test, which is the process working.
2. **`run_logged` mislabels successful reads.** It treats any result containing "denied by policy" as `REJECTED_BY_POLICY`. Our own ADRs contain that phrase, so a plan step reading them would be logged as a rejection. Not changed here; the engine logs its own exact status instead. Worth a separate fix.
3. **The 120s default `timeout_seconds` includes human think time at `[y/N]` prompts.** Fine for read-only plans; likely too short for a write plan where you read diffs. Documented, and `--timeout` overrides it.
4. **A plan's `git_commit` could cause data loss on rollback (found 2026-10-10, fixed).** Running a real write plan end to end in the sandbox showed the M001 commit tool (`git add -A`) committing every untracked file in the project, not just the file the plan wrote. A later `jarvis rollback` (`git reset --hard`) then deleted those files from disk, because the checkpoint commit did not contain them. My design note ("untracked files are safe, reset --hard leaves them alone") was true only until a commit made them tracked. The preview also showed them as a diffstat, not as deletions. Fixes: plan commits stage only plan-written files (`core/plan_commit.py`, ADR-0005 h); a commit step needs an earlier write step; the rollback preview lists files to be deleted. 12 regression tests added; 3 of them fail if the old behaviour is restored. All 330 tests pass in the sandbox. Re-verified on the ZBook on 2026-10-10 (Test C below).

## Review of commit 0a2b4bc by Antigravity (2026-10-11): five findings, all reproduced, fixed in `854ab89`
None of these lose data. What a plan commits was always exactly its own files (explicit pathspecs); the defects were in what the human is *shown* before the `[y/N]`, in plan validation, and in rollback state. The first four are why the preview is a consent surface worth testing.
1. **Preview mangled file names (Medium).** `GitInspector._run()` strips its output, which removed the leading space of the first `git status --short` line, so slicing at column 3 turned `xbar.txt` into `bar.txt`. A stray file could vanish from "NOT part of this commit", or a committed file be listed as excluded. Now parses `--porcelain=v2` (lines never start with a space) and uses the plan's own path set as "included".
2. **New directories collapsed (Medium).** A planned file in a new directory showed as excluded because git printed `?? tests/`. Now `--untracked-files=all`.
3. **Multi-commit plans (Medium).** `[write, commit, commit]` passed validation and then blocked at run time; later commits were also handed every earlier write. Both now look only at steps since the previous commit.
4. **No preview for a new file (Low).** `git diff` is empty for untracked files, so the prompt showed no content. A preview is now built from the file itself (symlinks are not followed).
5. **State-only rollback (Medium).** A plan that only created untracked files and then failed could not be rolled back: rollback said "Nothing to roll back" and left the plan `BLOCKED`. It now previews, asks `[y/N]`, resets the plan, and states that the untracked files remain on disk.

10 regression tests added; 8 fail on the old code (mutation-checked). The other two guard behaviour that must not change: no content read through a symlink, and "Nothing to roll back" when no plan is stuck. Sandbox: 384 passed on the fix branch, 393 combined with Antigravity's 9 integration tests (`c013597`). **Not yet run on the ZBook.**

## Gemini security audit of M002 and Antigravity's review of 854ab89 (2026-10-11)
Every finding below was reproduced before it was rated or fixed. Gemini labelled all of its findings "not reproduced"; I re-rated two of them lower.
| Finding | Source | Rating | Status |
|---|---|---|---|
| Repo config runs code during `git status` / `git diff` (`core.fsmonitor`, diff `textconv` drivers, hooks such as `post-index-change`). Gemini's suggested flags would have closed only two of the three. | Gemini | Medium (Gemini: High; `git clone` does not copy config or hooks, so it needs a folder with a hostile `.git`) | Task 004 (Codex), `tools/git.py` |
| Symlink swapped under a write while its `[y/N]` is open | Gemini | Low (Gemini: High; needs workspace write access you already have) | Task 004 (Codex), `tools/fs.py` |
| Rollback destroys an edit made while its `[y/N]` was open | Gemini | Medium | **Fixed**, `231e55b`; the preview is re-taken just before `reset --hard` and the rollback refuses if it differs |
| Parent environment (API keys) inherited by the pytest subprocess and echoed into saved evidence | Gemini | Low-Medium | Task 005 (Antigravity), allow-listed environment |
| Timed-out pytest leaves its child processes running (reproduced on Linux; Windows not yet tested) | Gemini | Low-Medium | Task 005 (Antigravity), kill the process tree |
| `GitInspector._run` decodes git output with the locale codec (cp1252 on the ZBook): non-ASCII file names mangle the commit preview, and a name containing byte 0x9d (for example a curly quote) crashes it. My `core.quotepath=off` exposed it, but any diff containing such a byte is affected. | Antigravity | Medium (Antigravity: High) | **Fixed** by me, out of order: `_run` now decodes UTF-8 (`encoding="utf-8", errors="replace"`). First confirmed on the ZBook when `test_preview_lists_non_ascii_names_correctly` failed there with `UnicodeDecodeError` / `AttributeError`; I had merged that test before its fix, which was my mistake. |
| `git status` C-quoted names with a quote or tab were mis-parsed | Antigravity | Low | **Fixed**: `--porcelain=v2 -z` |
| `_new_file_preview` read through a symlinked parent or junction | Antigravity | Low (a plan cannot reach it; the engine blocks the write) | **Fixed**: whole path resolved and kept inside the project |
| `_new_file_preview` read the whole file; printing an unencodable character crashed on a cp1252 pipe | Antigravity | Low | **Fixed**: bounded read; `main()` makes console output replace unencodable characters |

Open decision, left at the safe default: a plan's `git_commit` still runs the repository's own hooks (for example `pre-commit`). Their code is not shown at the `[y/N]`. Documented rather than blocked, because hooks are normally wanted in your own repositories.

## Antigravity's Task 005 review of 231e55b and 7f52fc1 (2026-10-11)
All four open findings reproduced in the sandbox with a failing test first; fixed in `489939e`.
| Finding | Rating | Fix |
|---|---|---|
| F1: the rollback re-check compared `diff --stat`, which counts lines, so an edit with the same line count passed and `reset --hard` destroyed it | High (data loss; I agree) | snapshot now includes a SHA-256 of `git diff --binary <commit>` and the tracked status |
| F2: a branch created at the same commit while the prompt was open: `reset --hard` moved the new branch | Medium | snapshot includes `branch --show-current` |
| F3: the state-only rollback path skipped the re-check | Low | shared `_refuse_if_changed` used by both paths |
| F5: a staged rename hid the old path from "NOT part of this commit" | Low | `--no-renames` in `commit_plan_paths` |
F4 (the non-ASCII test needing the UTF-8 decode) was already fixed in `938aa3d`. 5 new tests: 4 fail on the old code, 1 guards against the snapshot being too sensitive. Mistake worth recording: my first re-check (`231e55b`) was tested only with an edit that changed the line counts, so it passed its own test. The new test uses an edit with identical counts.

## Antigravity's review of 489939e (2026-10-11): four more findings, all reproduced, fixed
`489939e` and the Gemini/Task 005 fixes were merged to `main` and passed on the ZBook (417 passed, 8 skipped) before this review came back. Each finding below failed a new test on the code as merged.
| Finding | Rating | Fix |
|---|---|---|
| F1: an edit staged and then reverted on disk left the working tree equal to the checkpoint, so rollback said "nothing to roll back" and left the staged edit | Medium (no data lost; the plan is reset and a re-run is refused until the index is cleaned) | the summary now includes `diff --cached --stat`, so a staged difference counts as something to reset |
| F2: the commit preview showed no diff for a plan file that was already staged (`git diff` is working tree vs index) | Medium: a gap in the consent surface | diff against `HEAD`, which is what `git commit -- <paths>` records |
| F3: with `core.autocrlf=true`, a line-ending-only edit during the prompt left the digest unchanged | Low | digest computed with `-c core.autocrlf=false` |
| F4: a git error or timeout during the re-check escaped as a bare error with no audit row | Low | caught, logged as FAILURE, and reported as "Nothing was changed" |
Also from that report: the 8 ZBook skips are 4 symlink tests (Windows needs Developer Mode), 1 quoting test (illegal Windows file names) and 3 of Codex's hardening tests that use POSIX marker scripts. So Task 004's git-config protections have had no real Windows run. A follow-up task for Codex covers that. `AGENTS.md` invariant 3 now lists the only permitted exceptions to "writes go through `write_file`".
Lesson: a test that proves the new code works is not a test of what the old code missed. F1 here is the same family as the earlier `--stat` bug: each fix tested the case reported, not the neighbouring ones (index vs working tree, line endings). The new tests probe the neighbours.

## Verified on the real ZBook (Windows 11, Python 3.14, 2026-10-09)
- `pytest tests/`: **316 passed, 2 skipped**. The 2 skips are the symlink tests (`test_engine_core.py`, `test_path_boundary.py`), which Windows skips without Developer Mode or admin rights; 316 + 2 = 318.
- Read-only plan (`list_directory` + `read_file`): `plan-create`, `plan-run`, `plan-show` all behaved as designed, plan `COMPLETED 2/2`, no checkpoint (correct for read-only).
- Path-escape plan (`read_file ../../Windows/win.ini`): refused before any read, plan `BLOCKED` with reason `POLICY`, evidence saved, nothing rolled back or deleted. Note the path resolved to `C:\Users\USER\Windows\win.ini` (inside the user's profile, not the real `C:\Windows`); the check blocks on "resolved path outside the workspace root", so this still exercises the rule.
- Setup note: the `jarvis` console command on that machine was a stale install (`jarvis.cli`), and Python 3.14 there has no pip. Run via `$env:PYTHONPATH = "src"; python -m jarvis.interface.cli ...` instead.

## Verified on the real ZBook, write path (2026-10-10, merged engine + verifier branch, `fd47fca`)
- `pytest tests/`: **354 passed, 2 skipped** (356 total; the 2 skips are the same Windows symlink tests). This includes the 12 plan-commit regression tests.
- **Test A, write and verify:** diff shown, `[y/N]` accepted, `verified (file_content)`, plan `COMPLETED`, checkpoint recorded.
- **Test B, verification fails on purpose:** plan `BLOCKED` (`VERIFICATION`), verifier evidence shown, "Nothing was rolled back or deleted", the written file left on disk.
- **Test C, edit + commit + rollback, with an unrelated untracked file present:** the commit preview listed only `README.md` as "WILL be committed" and the other files under "NOT part of this commit"; the commit contained only `README.md`; after `rollback`, `README.md` was back to its original text and the unrelated untracked file still existed. This is the scenario that previously lost files.
- **Test D, write outside the project:** `BLOCKED` (`POLICY`) before any write prompt; the target file was not created.

## Not done / not verified
- Not run manually on the ZBook: the rollback preview's "Files that will be DELETED from disk" section (Test C edited an already-tracked file, so it had nothing to list). It is covered by an automated test only.
- Not run inside a real plan on the ZBook: the `pytest_target` verifier (unit-tested by Stream B, see build log 0017).
- `core/verifier.py` (Stream B, Antigravity) is now merged into this branch; the engine picks up `VERIFIERS` from it automatically. Its own record is build log 0017.
- No concurrency guard between two simultaneous `plan-run` processes.
- No `task-done` command: a completed plan leaves its task `IN_PROGRESS` (ADR-0005 e).
- Pushed to `origin/feature/claude-m002-engine` once GitHub write access was granted to the Claude app. `main` is untouched; Lone holds merge authority.
