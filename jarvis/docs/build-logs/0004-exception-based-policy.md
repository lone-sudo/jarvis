# 0004 — Exception-Based Policy Model + Windows Path Coverage

**Date:** 2026-09-20
**Contributors:** Lone, ChatGPT (via Gemini's synthesis), Gemini, Claude (fix + verify)

## Context
Following build-log 0003, ChatGPT and Gemini both independently pushed back on the
deliberate boolean/exception split documented there — specifically on
`PolicyValidator.authorize_tool`/`authorize_provider` staying boolean. Their point:
a boolean return value can be ignored by a careless caller, and the action then
executes anyway. 2-against-1 on a design choice Claude made unilaterally — implemented
as requested.

## Changed

1. **`PolicyValidator.authorize_tool` / `authorize_provider` now raise `PolicyViolation`
   instead of returning `bool`.** Success is the absence of an exception; there is no
   return value left to forget to check. Verified directly: a simulated "careless"
   caller that does nothing with the call result now has execution halted by the
   unhandled exception, rather than silently proceeding.

2. **`tools/fs.py`, `tools/git.py`, `providers/base.py`** all updated to catch
   `PolicyViolation` at the point where a human-readable result is needed, converting
   it to the existing `"ERROR: Access denied by policy. ..."` string format that
   `tools/base.py::run_logged` already recognizes as `REJECTED_BY_POLICY`. This keeps
   the audit-log behavior from build-log 0002 unchanged while making the underlying
   authorization check itself fail-closed rather than fail-open.

3. **Windows drive-letter and UNC paths are now explicitly rejected**, closing the gap
   flagged (correctly) in the earlier review as untestable on this Linux sandbox.
   `resolve_safe_path` now checks the raw path string against a regex
   (`^[a-zA-Z]:[\\/]` or `^\\\\`) before any `pathlib` resolution happens — deliberately
   string-level, because on Linux `pathlib.Path("C:\\foo")` is just an ordinary relative
   filename (backslash isn't a separator on POSIX), so relying on `Path.is_absolute()`
   alone would pass these silently in CI/sandbox while correctly rejecting them on the
   actual Windows target machine. This makes the check platform-independent by
   construction and honestly testable here, not deferred to "verify manually on the
   ZBook" as build-log 0003 had to say.

## Tests
- `tests/test_policy.py` rewritten for the exception-based API (`pytest.raises
  (PolicyViolation)` instead of asserting a boolean), plus 4 new parametrized tests
  for Windows drive-letter (`C:\...`, `C:/...`) and UNC (`\\server\share`) paths —
  now genuinely passing, not skipped or platform-gated.
- 37 tests total (32 previous + 5 new), all passing.

## Verified
- Live reproduction of the exact failure mode this fixes: called `authorize_tool` with
  a DANGEROUS tier and with a Windows-style path, confirmed both raise immediately
  rather than returning a value a caller could ignore.
- Full existing test suite (path boundary, SAFE_WRITE confirmation/rejection, provider
  state transitions, crash recovery) re-run and passing unchanged — the exception
  switch did not alter any observable behavior at the CLI/tool-output level, only the
  internal failure mechanism.

## Not changed
- The `REJECTED_BY_POLICY` string convention in the executions log — still triggered
  by `run_logged` detecting "denied by policy" in a tool's returned string, now fed by
  a caught `PolicyViolation` instead of a checked boolean. No schema change needed.
