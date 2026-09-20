# ADR-0001: Hybrid Storage — SQLite + Markdown

**Status:** Accepted
**Date:** 2026-09-20

## Context
Jarvis needs robust, uncorrupted machine state (sessions, tasks, executions) without
sacrificing human-readable, git-diffable project memory.

## Decision
- SQLite (`state/`): sessions, tasks, executions, checkpoints, registered projects.
  Structured, queryable, integrity-enforced via foreign keys.
- Markdown (`memory/`): per-project notes file, human-authored and readable, git-versionable.

## Consequences
- State updates are atomic; context assembly reads structured records first, Markdown
  for qualitative summaries.
- Foreign key constraints must be re-enabled per SQLite connection (not persisted by
  SQLite across connections) — enforced in `state/database.py::get_connection`.
