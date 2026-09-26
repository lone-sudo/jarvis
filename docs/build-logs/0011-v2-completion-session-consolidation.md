# 0011 — V2 Completion: Session/Task Consolidation

**Date:** 2026-09-25
**Contributors:** ChatGPT, Gemini (design), Lone (governance-rule decision), Claude (build + verify)

## Context
The last explicitly-deferred item from the original roadmap's V2 scope: session/task
consolidation, the "nightly wrap-up" concept (brief section 26) and memory consolidation
(section 17). Same pattern as inbox consolidation: manual trigger, preview-first, atomic
write-then-mark, archive/flag rather than delete.

## The one real disagreement, resolved by majority vote
Gemini proposed adding `project_key` to `sessions` via a new migration, backfilled from each
session's oldest task. ChatGPT and Claude both preferred deriving ownership from the session's
tasks at consolidation time, with no schema change — every task already has a mandatory
`project_key` (`NOT NULL` in the original schema), so a session's ownership is simply "do all
its tasks agree on one project." Per Lone's standing governance rule (majority wins on a genuine
2-1 split), ChatGPT + Claude's position was adopted. Everything else converged unanimously:
`COMPLETED`-only trigger (no `SUSPENDED`/`ABORTED` handling yet), atomic write-then-mark, no
scheduler, and ChatGPT's explicit ownership test matrix (same project / mixed / none / completed-
with-zero-tasks) plus an idempotency requirement.

## What was built
- `state/session_consolidation.py`: pure functions — `determine_ownership` (a session is
  eligible only if it has ≥1 task and all tasks share one `project_key`; anything else returns
  a specific ineligibility reason, never a guess), `build_consolidated_note` (PROJECT STATE /
  task list / outcome summary, per the original brief's section 17 sketch).
- `migrations/004_session_consolidated_flag.sql`: a single `consolidated` flag on `sessions` —
  the only schema change needed, since tasks never require their own flag (re-querying by
  `consolidated = 0` already prevents a consolidated session's tasks from being reconsidered).
- `interface/cli.py::cmd_session_consolidate`: discover eligible sessions → preview (project,
  task list with statuses, proposed note) → `[y/N]` confirm → write to `ProjectMemory` → mark
  consolidated ONLY on write success.

## Two real bugs found in the EXISTING test suite while verifying this feature, not in the new code
Adding migration 004 broke 2 of 6 existing migration tests — investigated rather than just
patched to pass:

1. **The migration runner had no protection against duplicate migration numbers.** Two existing
   tests injected a file literally named `003_broken.sql` for testing failure/retry behavior —
   which now collided with the real `003_network_allowlist.sql` (didn't exist when those tests
   were written). The runner silently applied whichever sorted first and **skipped the other
   forever**, with no error at all. This is a genuine landmine for any real project: two
   contributors independently adding a same-numbered migration would corrupt schema state
   silently. Fixed: `_discover_migrations` now raises `MigrationError` immediately on any
   duplicate number, naming both colliding files.
2. **The "legacy database" migration test was itself testing an impossible scenario.** It built
   its simulated legacy database by combining *all currently-discovered* migrations via raw
   `executescript`, including migration 004's `ALTER TABLE` (not idempotent, unlike 001-003's
   `CREATE TABLE IF NOT EXISTS`). Since the runner then tried to re-apply 004 against a database
   that already had that column baked in, it correctly failed with "duplicate column" — revealing
   that the test no longer represented a real scenario, not a bug in migration 004. A genuinely
   legacy pre-migration-system database can only ever have the 001+002 shape (that's literally
   when the migration system was introduced); anything containing 003+ content would necessarily
   have gone through the runner already, meaning it already has `schema_version` tracking. Fixed
   the test fixture to build its "legacy" simulation from only 001+002, matching reality, and
   renamed the injected broken-migration tests to use `999_broken.sql` to avoid the same
   collision going forward, with dynamic (not hardcoded) version assertions so adding future
   migrations won't silently break these tests again.

## Verified — with real scenarios, not just assertions
- **All four ownership cases**, live: same-project session (2 tasks, both DONE) → consolidated
  correctly, note written to the right project's `JARVIS_NOTES.md`; mixed-project session →
  flagged and skipped, nothing touched; zero-task completed session → flagged and skipped;
  still-`ACTIVE` session → excluded entirely from consideration (never even shown).
- **Idempotency**: ran `session-consolidate` twice — the second run correctly found only the
  still-eligible (flagged) sessions, the already-consolidated one didn't reappear, no duplicate
  note was written.
- **Atomicity**: simulated a `ProjectMemory.append_note` failure during a confirmed
  consolidation — confirmed the session's `consolidated` flag stayed `0` despite the user having
  answered "yes." Nothing silently half-done.
- **The new duplicate-migration-number guard**: manually constructed a real collision (a fake
  `003_evil_duplicate.sql` alongside the real `003_network_allowlist.sql`) and confirmed
  `apply_pending_migrations` raises immediately with a clear message naming both files, rather
  than silently skipping one.
- 137 tests total (125 previous + 12 new for session consolidation), all passing, including the
  2 previously-broken migration tests now fixed for the right reason rather than patched around.

## Not built (per the converged, still-frozen boundaries)
No handling for `SUSPENDED` or `ABORTED` sessions — deferred, consistent with "smallest useful
thing first." No scheduler. No `project_key` column added to `sessions` (per the majority
decision above).
