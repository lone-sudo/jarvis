# 0003 — Policy Boundary Hardening (Gemini's M001/V1 GitHub review)

**Date:** 2026-09-20
**Contributors:** Lone, Gemini (review), Claude (fix + verify)

## Context
Gemini reviewed the canonical repo directly on GitHub (not a zip) and found real
implementation gaps in the policy boundary. All confirmed by reproduction before
fixing, not taken on faith.

## Fixed

1. **`--content-file` read before policy check (the important one).** `cmd_write_file`
   previously called `open(args.content_file)` directly — before `FileSystemInspector
   .write_file()` (and therefore before any policy check) ever ran. Live-fire tested:
   `jarvis write-file ... --content-file /etc/passwd` previously would have read and
   potentially written /etc/passwd's contents into the workspace, printing them in the
   diff preview along the way. Now routes through `SecurityPolicy.resolve_safe_path()`
   before the file is ever opened. Confirmed blocked in a real run.

2. **`register_project()` had no boundary check.** A project could previously be
   registered with a `relative_root` of `../../etc` and nothing would stop it — every
   git/filesystem/memory operation against that project would then operate outside the
   workspace. Now raises `PermissionError` at registration time. Confirmed blocked in
   a real run (`jarvis init-project evil ../../etc` now fails cleanly).

3. **Introduced one canonical path-boundary helper:** `SecurityPolicy.resolve_safe_path()`.
   `is_path_safe()`, `ProjectMemory._notes_path()`, and `StateTracker.register_project()`
   all now route through it, instead of each reimplementing path concatenation + boundary
   logic separately (which is exactly how `ProjectMemory` ended up with no check at all
   before this fix).

4. **Documented, not changed, the boundary-vs-tier exception/boolean split.** Boundary
   checks (`resolve_safe_path`) are exception-based; tier/DANGEROUS/$0-spend checks
   (`PolicyValidator.authorize_tool/authorize_provider`) stay boolean, because every
   existing tool call site already converts a boolean denial into a descriptive
   `"ERROR: ..."` string that `run_logged` recognizes as `REJECTED_BY_POLICY`. Converting
   that layer too would touch every tool's return contract without adding safety, since
   the actual structural invariant is now centralized in `resolve_safe_path`.

5. **Task status transitions are now validated, not just membership-checked.**
   `update_task_status` previously only checked "is this a known status" (`NOT_REAL`
   rejected) but allowed any known-status jump, including nonsensical ones like
   `DONE -> PENDING`. Added a small `ALLOWED_TRANSITIONS` map (deliberately not a full
   state-machine framework, per Gemini's own scoping note) and validated against it.

6. **Repository visibility corrected to Private** (was public — flagged in the same
   review; fixed by Lone directly in GitHub settings).

## New tests (`tests/test_path_boundary.py`)
Absolute path outside workspace, single and nested `../` traversal, symlink escape
(real filesystem symlink, not simulated), project registration outside workspace
(both relative-traversal and absolute-path forms), the exact `--content-file` attack
Gemini described, and both the reject and accept paths for Markdown memory.

**Honest scope note:** Windows drive-letter paths (`C:\...`) and UNC paths
(`\\server\share`) are not meaningfully testable in this Linux sandbox — on Linux
they're just unusual relative filenames, not absolute paths, so a passing test here
wouldn't prove anything about real Windows behavior. Flagged for manual verification
once this is running on the ZBook, rather than faking coverage for it now.

## Verified
- 32 automated tests pass (24 previous + 8 new... actually 11 new in
  test_path_boundary.py, replacing/extending prior coverage — see test file).
- Live-fire reproduction of the exact content-file attack: blocked.
- Live-fire reproduction of the project-registration escape: blocked.
- Confirmed legitimate in-workspace `--content-file` usage still works end-to-end.

## Not changed (per Gemini's own recommendation)
- `core/` remains empty — correct to let real tool/state/provider interfaces settle
  before inventing an orchestration API.
- No move to a full state-machine framework for task transitions — the small allowed-
  transitions map is intentionally minimal.
