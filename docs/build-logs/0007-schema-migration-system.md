# 0007 — Schema Migration System

**Date:** 2026-09-21
**Contributors:** ChatGPT, Gemini (design), Claude (build + verify)

## Context
Flagged as a gap in build-log 0006 (V2): no schema migration system, meaning a
future change to an *existing* table's columns would silently fail to apply on
upgrade (`CREATE TABLE IF NOT EXISTS` doesn't add columns). Built now, ahead of V3,
per ChatGPT's explicit sequencing.

## Design (per ChatGPT + Gemini, no framework)
- `schema_version` table: single row, `version INTEGER`.
- Numbered SQL files in `state/migrations/` (`001_initial.sql`, `002_inbox.sql`),
  applied strictly in numeric order (not lexical — `10` sorts after `2`, verified).
- `state/migration_runner.py`: reads current version, applies each pending migration
  in its own transaction, advances `schema_version` only on that migration's commit.
- Migration files are trusted application resources shipped with Jarvis, never
  user-controlled or externally fetched.

## A real finding during implementation, not assumed
Tested `sqlite3.Connection.executescript()`'s transaction behavior empirically
before trusting it for migrations, since atomicity was the whole point. Confirmed
it does **not** respect an enclosing manual `BEGIN`/`ROLLBACK` the way expected —
a multi-statement script with a failing second statement left the first statement's
effect committed anyway, even inside an explicit transaction wrapper. Reproduced
directly:
```
BEGIN; executescript("INSERT ...; INSERT <invalid>;"); # fails
ROLLBACK; # raises "cannot rollback - no transaction is active"
# yet the first INSERT's row is still there
```
This would have silently broken the exact atomicity guarantee ChatGPT's review
required, had it shipped. Fixed by executing each migration statement individually
via `conn.execute()` inside a manually managed transaction (`conn.isolation_level
= None`, explicit `BEGIN`/`COMMIT`/`ROLLBACK`) — verified this pattern actually
rolls back correctly before writing the real runner.

## A second bug caught before any test ran
`state/migrations.py` (the runner module) and `state/migrations/` (the directory
of SQL files) can't coexist under the same Python package — a naming collision
that would have broken imports. Caught while writing the code, before packaging;
renamed the runner to `state/migration_runner.py`.

## `schema.sql` deprecated, not deleted
Old `schema.sql` no longer read by any code (superseded by the migrations
directory) but kept in place with a comment pointing to its replacement — a
historical reference of the pre-migration-system schema shape, not a second
source of truth.

## Verified (all four scenarios ChatGPT specifically asked for)
1. **Fresh database:** all migrations apply, all 7 tables present, version lands
   at the latest number.
2. **Legacy V2 database compatibility — the one that matters most:** built a
   database the *old* way (`executescript` against the old combined schema, with
   real session/project data inserted, no `schema_version` table — exactly what
   Lone's real ZBook database looks like right now), then ran the migration
   system against it. Confirmed the pre-existing data survived unchanged and
   `schema_version` correctly caught up to 2 without re-creating or altering
   anything (the `IF NOT EXISTS` migrations are no-ops against already-existing
   tables).
3. **Mid-migration failure:** a deliberately broken migration 003 (one valid
   statement, one invalid) correctly rolled back — the valid statement's table
   was confirmed **absent** afterward (no partial application), and
   `schema_version` stayed at 2, not advanced to 3.
4. **Retry after fix:** same database, same migration file corrected, ran again
   — succeeded cleanly, `schema_version` reached 3, the corrected migration's
   data was present.

6 new automated tests in `tests/test_migrations.py` covering all of the above
plus idempotency (running twice is a no-op the second time) and numeric-vs-lexical
ordering. Full suite: 56 passing (50 previous + 6 new), confirming the swap from
`executescript` to the migration runner didn't change any existing behavior.

## Not yet built
V3 (controlled URL fetch) — this was explicitly sequenced first, per ChatGPT's
"proceed with the migration implementation first, then V3." Migration system is
done and reported; V3 design is separately converged and awaiting this report
before implementation begins.
