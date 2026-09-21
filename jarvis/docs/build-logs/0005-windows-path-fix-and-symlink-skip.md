# 0005 — Fix: Overly Aggressive Windows Path Rejection + Symlink Test + pytest Setup

**Date:** 2026-09-20
**Contributors:** Lone (ran the actual test suite on the ZBook, caught both bugs), Gemini (diagnosed both), Claude (fixed + verified)

## Context
Lone ran `pip install -e .` and `pytest tests/ -v` on the real ZBook for the first
time. This surfaced two real problems that the Linux sandbox couldn't catch —
exactly the value of testing on the actual target machine, not just in CI.

## Bug 1 (real regression, introduced in build-log 0004): blanket Windows path rejection

The regex-based fix in 0004 rejected *every* Windows-style absolute path
(`C:\...`, `C:/...`, `\\server\share`) unconditionally, regardless of whether it
resolved inside the workspace. On Linux this only ever affected deliberately
Windows-shaped test strings, so it looked correct. On real Windows, the workspace
root itself and everything inside it are Windows-style absolute paths — so this
rejected legitimate, in-workspace paths outright, including pytest's own
`tmp_path` fixture. 4 of the 5 failures Lone hit were this bug.

**Root cause:** conflating "looks like a Windows path" with "is unsafe." The
actual invariant that matters is "does it resolve inside the workspace root,"
which is true or false independent of what the path looks like.

**Fix:** replaced the blanket regex with a narrower check using `ntpath.isabs()`
(Windows path semantics, available on any host OS) compared against the *native*
`pathlib.Path.is_absolute()` for the current host. A path is only rejected up
front when it looks like a Windows absolute path but the native pathlib on this
machine does *not* also recognize it as absolute — meaning we're on a non-Windows
host being asked to resolve something that could never legitimately live inside a
POSIX workspace anyway. On real Windows, both checks agree, so the path falls
through to the normal resolve-and-contain logic — the same logic that correctly
allowed things before 0004 and correctly rejects genuinely out-of-bounds paths
like `C:\Windows\System32\...` that don't resolve inside the configured
workspace root.

Confirmed via `ntpath.isabs()` against realistic Windows paths (workspace root,
pytest temp dirs) — both correctly identified as legitimately-absolute-and-inside-
workspace, so they'd pass on real Windows exactly as intended.

## Bug 2: symlink test fails with WinError 1314 on unprivileged Windows

Creating a symlink on Windows requires Administrator privileges or Developer Mode
enabled — a real Windows limitation, not a code bug. `test_symlink_escape_rejected`
now wraps the `symlink_to()` call in try/except and calls `pytest.skip()` with a
clear reason if it can't create the link, rather than failing. The test still runs
for real wherever the capability exists (Linux, macOS, or Windows with Developer
Mode on) — it isn't hardcoded to skip on Windows specifically, since some Windows
setups do support it.

## Setup improvement (not a bug, but worth fixing): `ModuleNotFoundError: No module named 'jarvis'`

Both ChatGPT and Gemini independently hit this on a fresh venv before running
`pip install -e .`. Added `[tool.pytest.ini_options] pythonpath = ["src"]` to
`pyproject.toml` so `pytest` finds the package directly without needing the
editable install or a manual `PYTHONPATH` first — removes a step that's tripped
up two out of two people who've set this up fresh so far.

## Verified
- Full suite re-run on Linux: 37/37 passing (unchanged count — the fix didn't
  remove any real coverage, it corrected the logic the coverage was testing).
- Windows-path rejection tests (`test_windows_style_absolute_paths_rejected`)
  still pass on Linux, via the same `ntpath` vs. native-`Path` comparison,
  proving the logic is sound without needing to fake it.
- Manually traced the exact Windows scenario (pytest's `tmp_path`, and the real
  configured workspace root) through the new logic and confirmed both are
  correctly treated as in-bounds.

## What still needs Lone's confirmation
This fix is logically verified but the previous fix was too — and it broke on
real Windows. **This one specifically needs to be re-run on the ZBook** before
it's trusted, not just accepted from sandbox verification. The point of build-log
0004's mistake is exactly that "passes in the Linux sandbox" isn't sufficient
proof for Windows-specific logic.
