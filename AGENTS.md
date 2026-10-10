# AGENTS.md: shared rules for every AI working on Jarvis

Read this first. It is the single source of truth for Claude, Codex, Antigravity, Gemini and ChatGPT.
`CLAUDE.md` only points here. If a chat message disagrees with this file, stop and ask Lone.
Do not paste long context into a session to "bring you up to speed": read this file, the ADR for the
subsystem you touch, and the code. Past decisions live in `docs/adrs/`. Do not re-derive them.

## What Jarvis is
A personal AI operating layer for Lone (Magichu Njoroge). Jarvis owns machine state, memory, task
continuity and tool execution. AI models are replaceable reasoning backends, never the core.

## Non-negotiable invariants
1. **$0 automatic spending.** `MAX_AUTO_SPEND_USD = 0.00` in `policy/rules.py`. No automatic paid API
   fallback, no auto top-ups, no pay-per-use trigger without explicit human approval (ADR-0002).
2. **Workspace containment.** All file operations go through `SecurityPolicy.resolve_safe_path()`,
   contained to `JARVIS_WORKSPACE_ROOT`. Path traversal is blocked (ADR-0003).
3. **Confirmed mutations.** `write-file` and `git-commit` show a diff and need an interactive `[y/N]`.
4. **Dual storage (ADR-0001).** SQLite for machine state; Markdown for human-readable project memory.
5. **Real-machine verification.** Sandbox-verified is not real-machine-verified. Nothing is "done"
   until Lone has run it on his HP ZBook (Windows 11). Say which of the two you did.
6. **Stdlib first.** Minimal dependencies. Core code uses the Python standard library.
7. **Evidence is never destroyed.** A failed step never silently rolls back or deletes. Diagnostics are
   saved, the plan goes `BLOCKED`, and only a human runs `jarvis rollback`.

## Anything that routes AI traffic, holds keys, or runs code needs review first
This includes AI gateways and routers, proxies, "auto-fallback" tools, third-party skills and MCP servers.
Before adopting one, read it and check it against invariants 1 and 2. A tool that can fall back to a
paid provider, or that stores or forwards API keys, is not adopted without Lone's explicit approval.

## Who does what (single writer per subsystem)
| Who | Role | Owns |
|---|---|---|
| Lone | Product authority, live-machine tester | **Sole merge authority on `main`** |
| Claude | Core engine maintainer, final diff reviewer, writes task specs | `core/plan.py`, `core/engine.py`, `core/checkpoint.py`, `state/plan_store.py`, CLI plan commands |
| Antigravity | Verifier and test-harness engineer | `core/verifier.py`, `tests/test_verifiers.py`, integration and mutation harnesses |
| Codex | Bounded, well-specified tasks off the engine's critical path; second independent reviewer of security-sensitive diffs | Whatever a task spec assigns it, and nothing else |
| Gemini | Security audit: path boundaries, policy, secret redaction | **Review only. No code.** |
| ChatGPT | Strategy, task breakdown, UX | No code on the engine |

Rules that follow:
- **Only the owner edits a file.** Need a change in someone else's file? Write the request, do not edit it.
- **The author never reviews their own code.** Every non-trivial change is reviewed by at least one other model.
- **Reviewers report, they do not rewrite.** Findings: severity, file and function, how to trigger it,
  the fix in words.
- **Reviewers reproduce before claiming.** Back a finding with a run, or label it "not reproduced".

## Branches and merging
- `main` is protected and fast-forward only, after the full test suite passes. Only Lone merges.
- One feature branch per stream, named `feature/<owner>-<milestone>-<topic>`, cut from the current
  integration branch. Never push to `main`. Never force-push a shared branch.
- Commit messages say what and why. Each tool adds its own co-author line.

## Testing and running (Windows 11 is the real target)
- Full suite: `python -m pytest tests/ -v`. Pytest finds `src/` itself (`pythonpath` in `pyproject.toml`),
  so no install is needed for tests.
- Run the CLI without installing: `$env:PYTHONPATH = "src"; python -m jarvis.interface.cli <command>`.
  The installed `jarvis` console command can be a stale old install. Prefer the line above when in doubt.
- Two symlink tests skip on Windows without Developer Mode or admin rights. That is expected.
- Report the exact count and where you ran it (Linux sandbox, Windows, or both). Never write "all tests
  pass" without the number.
- Tests must be platform-independent. Do not hardcode `C:/` paths or rely on Windows-only path semantics.

## Safety lessons already paid for (do not relearn them)
- A write can stay inside the workspace yet escape its project and become impossible to roll back.
  Mutating steps are confined to the task's project directory (ADR-0005 b).
- A plan's `verify` spec runs outside the four-tool allowlist, so it must be validated like a tool.
  Unrestricted pytest `args` could delete directories (`--basetemp`) or write files outside the project
  (`--junitxml`). Pytest args are allow-listed; absolute paths are rejected.
- `tools/base.py::run_logged` labels any output containing "denied by policy" as a rejection, even a
  successful read of a document that quotes the phrase. Known issue, not yet fixed.
- Tools report errors as strings starting `ERROR:`. A check that treats a git error as "no output"
  fails open. Fail closed on any `ERROR:`.

## Task hand-off format (keeps token cost low for everyone)
When Claude writes a task for Antigravity or Codex, it uses this shape and nothing longer:
1. **Goal**: one sentence.
2. **Branch** and **files you may edit** (everything else is read-only to you).
3. **Contract**: the interface to implement, or a link to the ADR section.
4. **Acceptance**: the tests that must pass, plus the behaviours to be added as new tests.
5. **Out of scope**: what not to touch.
6. **Report back**: branch, commit hash, test count and platform, and anything you were unsure about.

## Current state and where to look
- Architecture decisions: `docs/adrs/` (ADR-0005 is the execution engine and its verifier contract).
- Per-milestone history: `docs/build-logs/`. The narrative is `docs/JOURNEY.md`.
- Check `git log` and the latest build log for what is verified on the real ZBook and what is not.
- Non-goals: no Neo4j or RDF graphs; no uncontrolled all-night autonomous loops; no custom scrapers for
  TikTok or Instagram; no automated multi-model consensus councils (reviews are human-dispatched);
  no dual-machine (ZBook and Dell) code until M003.
