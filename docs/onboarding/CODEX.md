# Codex onboarding: Jarvis

You are joining a project that is already well along. Other AI tools (Claude, Antigravity, ChatGPT, Gemini)
have been working on it, and Lone (the owner) has so far talked mostly to ChatGPT. You have none of that
history. This file is the history you need, in the order you need it. Read it fully once; after that,
`AGENTS.md` is your rulebook.

## 1. What Jarvis is, in one minute
Jarvis is a **personal AI operating layer** for Lone, a data engineering and structural engineering
student in Kenya. Jarvis itself owns machine state, memory, task continuity and tool execution. AI models
(Claude, Codex, Gemini, ChatGPT, Ollama) are **replaceable reasoning backends**, never the core. A model
proposes; Jarvis decides what actually runs. That is why so much of the code is policy checks,
confirmations and saved evidence.

It is a Python 3.10+ command-line tool (`jarvis ...`), standard library first (one runtime dependency:
`pyperclip`; `pytest` for tests). The real machine is Lone's **HP ZBook, Windows 11**. Development also
happens on Linux sandboxes. **Windows is the target; Linux passing is not enough.**

## 2. The rules that cannot bend
These live in `AGENTS.md`. Memorise them, because every review and task is judged against them.
1. **$0 automatic spending.** No automatic paid-API fallback, no pay-per-use triggers.
2. **Workspace containment.** Every file operation goes through `SecurityPolicy.resolve_safe_path()`.
3. **Confirmed mutations.** `write-file` and `git-commit` show a diff and need `[y/N]`.
4. **Two storage kinds.** SQLite for machine state, Markdown for human-readable memory.
5. **Real-machine verification.** Nothing is "done" until **Lone** has run it on the ZBook. You can say
   "passes in my environment", never "done".
6. **Standard library first.** Do not add dependencies without asking.
7. **Evidence is never destroyed.** Failures are saved and shown; nothing is silently rolled back or deleted.

## 3. Who does what
| Who | Role |
|---|---|
| **Lone** | Product owner and live tester. **Only Lone merges to `main`.** |
| **Claude** | Maintains the execution engine (`core/plan.py`, `engine.py`, `checkpoint.py`, `plan_commit.py`, `state/plan_store.py`, the plan commands in the CLI). Writes your task specs and reviews your diffs before Lone merges. |
| **Antigravity** | Owns `core/verifier.py` and the test harness. |
| **Codex (you)** | Takes **bounded, well-specified tasks** away from the engine's critical path, and acts as a **second, independent reviewer** of security-sensitive changes you did not write. |
| **Gemini** | Security review only. No code. |
| **ChatGPT** | Strategy, task breakdown, UX. |

**One writer per file.** If you need a change in a file you do not own, write the request. Do not make the edit.
**Nobody reviews their own code.** That rule has already caught real bugs (see section 7).

## 4. Your first 20 minutes
Read, in this order:
1. `AGENTS.md`
2. `docs/JOURNEY.md` (the story of how it was built, mistakes included)
3. `docs/adrs/ADR-0002` (the $0 rule), `ADR-0003` (workspace boundary and execution tiers), `ADR-0005`
   (the execution engine, and the verifier contract at the end)
4. `docs/build-logs/0016` and `0017` (the latest work, what was found, what is verified)

Then run the tests to see the project is healthy in your environment:
```
python -m pytest tests/ -q
```
Expected: **356 tests**. On Windows without Developer Mode, **2 symlink tests skip**, so you see
354 passed, 2 skipped. Pytest finds `src/` by itself (`pythonpath` in `pyproject.toml`).

Run the CLI without installing anything (PowerShell):
```
$env:PYTHONPATH = "src"
python -m jarvis.interface.cli --help
```
Do not trust a bare `jarvis` command. On Lone's machine it was a stale old install.

## 5. The map
```
src/jarvis/
  core/        plan.py, engine.py, checkpoint.py, plan_commit.py (Claude); verifier.py (Antigravity)
  policy/      rules.py (SecurityPolicy, ActionTier), validator.py, network_policy.py, url_validation.py
  state/       SQLite: database.py, tracker.py, plan_store.py, migrations/ (numbered .sql), inbox, consolidation
  tools/       fs.py, git.py, network.py, base.py: the tools the policy layer gates
  providers/   AI provider adapters (manual clipboard, Gemini free tier)
  memory/      Markdown project memory and the content inbox
  interface/   cli.py (all commands), telegram_bot.py (read-only)
tests/         one test file per area; fixtures create temporary git repos and workspaces
docs/adrs/     frozen architecture decisions       docs/build-logs/  what was built, found, verified
```
**The execution engine (M002), in one picture:**
`Execution Plan -> Engine -> per-step policy check -> tool call -> targeted verification -> saved state`.
A plan is a short list of steps (max 5 by default, 120 s budget). Plan tools are exactly `read_file`,
`list_directory`, `write_file`, `git_commit`. Mutating steps must declare a `verify` spec and stay inside
the task's project folder. On failure the plan goes `BLOCKED`, evidence is saved, and only a human runs
`jarvis rollback --checkpoint <id>`.

## 6. How to be effective here
- **Stay inside the spec.** A task lists the files you may edit. Everything else is read-only to you,
  even if you can see a better design. Note it in your report instead.
- **Small diffs, one concern each.** Reviews are cheaper and safer that way.
- **Fail closed.** Tools signal errors with strings starting `ERROR:`. Treat any error as failure, never as "no output".
- **Write tests that work on every platform.** No hardcoded `C:/` paths. Python treats a Windows drive
  path as relative on Linux, and a rooted path like `/etc` may count as relative on Windows, so build absolute paths from `tmp_path`.
- **Reproduce before you claim.** A bug or finding needs a run that shows it. If you could not reproduce it,
  label it "not reproduced".
- **Ask, don't guess.** If a spec is ambiguous or two rules conflict, stop and ask Lone. Do not pick silently.
- **Report exactly.** Test counts with the platform ("354 passed, 2 skipped on Windows 11"). Never "all tests pass".
- **No new dependencies, no network features, nothing that can spend money.**
- **Never put secrets in code, logs or test output.** Diagnostics are saved to SQLite and printed.
- **Branches:** `feature/codex-<topic>`, cut from `main`. Never push to `main`. Never force-push.
- **Commits:** say what and why, and end with your own co-author line.

## 7. Mistakes already made (do not repeat them)
- A plan commit used `git add -A`, swept unrelated untracked files into the commit, and a rollback then
  **deleted them from disk**. Fixed; now a plan commits only the files it wrote.
- Passing arbitrary `pytest` arguments through a verifier could delete directories (`--basetemp`) or write
  files outside the project (`--junitxml`). Fixed with an allow-list and a ban on absolute paths.
- A "contained inside the workspace" check let a write escape its own project, where rollback could not undo it.
- Windows path rules differ from Linux. A regex that blocked paths broke legitimate Windows paths.
- Some tests only failed on Lone's real machine. That is why invariant 5 exists.

## 8. When you review someone else's diff
Check, in this order: (1) path handling: `..`, absolute paths, drive letters and UNC paths on Windows, symlinks;
(2) per-step authorization: can anything run without a fresh policy check; (3) fail-open errors;
(4) secrets in diagnostics; (5) anything that can delete or overwrite data, and whether the preview says so
plainly; (6) platform assumptions. Report each finding as: severity, file and function, how to trigger it,
the fix in words. **Report, do not rewrite.** An honest "I checked X, Y, Z and found nothing" is a good result.

## 9. How a task reaches you
Claude writes it in this shape, and your report mirrors it:
1. **Goal** (one sentence)  2. **Branch** and the **files you may edit**  3. **Contract**
4. **Acceptance** (tests that must pass)  5. **Out of scope**  6. **Report back** (branch, commit hash,
test count and platform, anything unsure)

Start every session with this line: *"Read `AGENTS.md` and `docs/onboarding/CODEX.md`. Tell me the seven
invariants and which files you may edit for this task, then begin."* If it cannot state them, it has not read them.

## 10. Task 001: `jarvis doctor` (your first job)
Lone's setup problems this week (a stale `jarvis` command, no pip, a wrong `PYTHONPATH`) are exactly what
this should catch.

1. **Goal:** add a read-only `jarvis doctor` command that diagnoses a broken setup, plus a "Windows setup"
   section in the README.
2. **Branch:** `feature/codex-doctor`, cut from `main`.
   **Files you may edit:** new `src/jarvis/interface/doctor.py`; new `tests/test_doctor.py`; the README
   (new section only); `src/jarvis/interface/cli.py` **only** to wire in the `doctor` subcommand.
3. **Contract:** print OK, WARN or FAIL for each check; exit non-zero only if something FAILs.
   - Python meets `requires-python` in `pyproject.toml`
   - `pip` available (`python -m pip --version`); missing pip is a WARN, not a FAIL
   - `git` available
   - `jarvis` imports, and any installed `jarvis` console command points at `jarvis.interface.cli:main`
     (stale = WARN, with the fix: `$env:PYTHONPATH = "src"; python -m jarvis.interface.cli`)
   - workspace root resolves, exists and is writable
   - database path resolves

   No network calls, no writes. Standard library only.
4. **Acceptance:** every check has a passing and a failing test (use monkeypatching), none with hardcoded
   `C:/` paths. The full suite passes on your platform (expect 356 tests, minus any symlink skips on Windows).
5. **Out of scope:** the engine, verifier, policy and network code, and any other change to `cli.py`.
6. **Report back:** branch, commit hash, test count and platform, anything you were unsure about.

## Glossary
**Plan / step:** a short list of tool calls the engine runs. **Tier:** how dangerous a tool is
(`OBSERVE`, `SAFE_WRITE`, ...). **Workspace root:** the folder Jarvis may touch (`JARVIS_WORKSPACE_ROOT`,
default `~/jarvis-workspace`). **Project root:** one registered folder inside it. **Checkpoint:** the git
commit a plan started from, saved so a human can roll back. **Verifier:** checks a step's own intended
result. **ZBook:** Lone's real Windows 11 machine, where "done" is decided.
