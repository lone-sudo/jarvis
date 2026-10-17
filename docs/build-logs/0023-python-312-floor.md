# 0023 — Raise the Python floor from 3.10 to 3.12

**Date:** 2026-10-17
**Contributor:** Claude
**Branch:** `feature/claude-python-312-floor` (cut from `main`; not merged)
**Status:** Approved by Lone on 2026-10-16. Sandbox-verified only; not yet run on the ZBook.

## Why
`Path.is_junction` exists only from Python 3.12. The link guard in `core/checkpoint.py` (`_is_link`) reads it
with `getattr(path, "is_junction", lambda: False)`, so on 3.10 and 3.11 a Windows junction looks like an
ordinary folder. Both the rollback guard and the plan `git_commit` guard (branch
`feature/claude-m002-1-commit-junction-guard`) depend on it. The ZBook runs 3.14, so the floor costs nothing there.

## Changed
- `pyproject.toml`: `requires-python = ">=3.12"`.
- `docs/onboarding/CODEX.md`: "Python 3.12+".
- `tests/test_doctor.py`: the fake requirement and fake versions moved up with the floor (same two
  behaviours tested: a satisfying version passes, a lower one fails).

## Not changed (on purpose)
- The `getattr(..., "is_junction", ...)` fallback in `_is_link` stays. It is harmless on 3.12+, and removing it
  is a separate cleanup that belongs with the junction-guard branch's owner review.
- No safeguard, policy or behaviour changed.

## Verified
Linux sandbox, Python 3.13: see the commit message for the exact count. Not run on Windows.

## For Lone on the ZBook
`python --version` should print 3.12 or higher, then `python -m pytest tests/ -q`, and `jarvis doctor` should
show the Python check as OK.
