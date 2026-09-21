# 0001 — Prototype Initialization

**Date:** 2026-09-20
**Contributors:** Lone, ChatGPT, Claude, Gemini

## What happened
Converged on a canonical architecture across three AI reviewers (hybrid storage, no
LiteLLM, no vector DB yet, monolith-first, $0 spend policy, policy-gated tools) and
built Milestone 001: repo scaffold, policy layer, SQLite schema + state tracker,
read-only git/filesystem tools, Markdown project memory, manual clipboard provider
adapter, and the `jarvis resume` ("where did I leave off?") capability.

## Bugs found and fixed during implementation (not caught in architecture review)
1. **FK enforcement silently off after the first connection.** SQLite does not persist
   `PRAGMA foreign_keys = ON` across connections; the original schema-init-only version
   meant every insert/delete after startup ran with FK checks disabled. Fixed by
   re-issuing the pragma on every `get_connection()` call.
2. **CWD-dependent database path.** A bare relative `"jarvis_state.db"` meant running
   the CLI from two different terminal locations created two silently disconnected
   databases. Fixed with a fixed path under `<workspace_root's parent>/.jarvis/`.
3. **`project_key` had no reliable link to a real directory.** The original `resume`
   command assumed `project_key` equals a workspace-relative path, which breaks the
   moment they diverge. Added a `projects` table and `init-project` / `register_project`
   to make that mapping explicit.
4. **`git` not installed crashed the CLI outright.** `FileNotFoundError` wasn't caught
   in the original subprocess call. Added explicit handling, plus checks for
   "directory doesn't exist" and "not a git repo" before shelling out.
5. **`pyperclip` import failure crashed the whole CLI on headless/no-clipboard systems**,
   not just the provider adapter. Wrapped the import and both copy/paste calls with
   fallbacks to manual print/type.
6. **No way to actually test the system without hand-writing Python.** The original
   CLI only implemented `resume`. Added `init-project`, `new-session`, `new-task`, and
   `note` subcommands so the whole loop is usable from the terminal.

## Verified this session
- 13 automated tests pass (`pytest tests/`), including an explicit crash-recovery test:
  create task in one `StateTracker` instance, discard it, reopen a fresh instance
  pointed at the same DB file, confirm the task is still there.
- Manual end-to-end run: registered a project, created a session and task, added a
  note, ran `jarvis resume` and got a correct recovery card with live git status.
- Path-traversal attempts (`../../../etc/passwd`) are correctly blocked by policy.
- Creating a task against a nonexistent session is correctly rejected by the FK
  constraint (proving fix #1 above actually works, not just compiles).

## Next
V1: confirmed-write tools (git commit, file write) behind `SAFE_WRITE` tier + explicit
confirmation; provider dispatch wired into task status transitions
(`AWAITING_USER` set/cleared automatically around `dispatch_prompt`).
