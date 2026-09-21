# ADR-0003: Workspace Boundary and Execution Tiers

**Status:** Accepted
**Date:** 2026-09-20

## Context
Even read-only tools can leak sensitive files (e.g. `~/.ssh`, `~/.aws`) via path
traversal (`../../`) if not bounded.

## Decision
- `ActionTier`: OBSERVE (read-only) / SAFE_WRITE (local mutation, confirmed) /
  DANGEROUS (always blocked in Milestone 001).
- `SecurityPolicy.get_workspace_root()` defines the one directory tree Jarvis may touch.
  Defaults to `~/jarvis-workspace`, not `~/projects`, to avoid colliding with an
  existing folder the user never intended to expose.
- `SecurityPolicy.is_path_safe()` resolves the target path fully (following `..` and
  symlinks) and checks it falls inside the workspace root before any tool acts.
- Every tool in `tools/` calls `PolicyValidator.authorize_tool(...)` before touching
  the filesystem or running a subprocess — this is structural, not optional per call site.

## Consequences
- Total containment during the prototype: nothing outside the workspace root is
  reachable even by accident.
- All paths stored in SQLite/Markdown are relative to the workspace root, in POSIX
  format — not absolute Windows paths — so state remains portable if a second machine
  (e.g. the Dell, once repaired) joins later.
