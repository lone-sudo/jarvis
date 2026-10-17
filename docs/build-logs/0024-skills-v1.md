# 0024 — Skills v1: text-only, hash-pinned skills (ADR-0006)

**Date:** 2026-10-17
**Contributor:** Claude
**Branch:** `feature/claude-skills-v1` (cut from `main` at `a26d018`; not merged)
**Status:** Built and tested in the Linux sandbox. Not run on the ZBook. Needs Antigravity's review (the author never reviews their own code).

## What it does
`jarvis skill list | show NAME | approve NAME | disable NAME`. A skill is `<workspace>/skills/<name>/SKILL.md`
plus optional flat `references/*.md`. Nothing is ever run, fetched or granted. The only thing written is state in
SQLite (migration 007: `skills`, `skill_events`); skill files are never written by this code (a test proves it).

## Rules enforced (ADR-0006)
- Frontmatter: only `name`, `description`, `license`, `compatibility`, `metadata`; `allowed-tools` and anything
  else refused by name. Name 1 to 64 chars, `a-z0-9-`, single hyphens, equals the folder name. Description 1 to 1024.
  A small strict parser (no YAML library): no block text, anchors, flow lists or maps, escapes, duplicate keys, tabs or BOM.
- Folder: only `SKILL.md` and `references/*.md`. `scripts/`, `assets/`, stray files and sub-folders refused.
  64 KiB per file, 256 KiB and 20 files per skill, UTF-8 only, NUL refused.
- Links and junctions refused at the skills folder, the skill folder and every entry (the path itself, not only its parents).
- Hash: SHA-256 over sorted POSIX paths and LF-normalised text with a NUL after every path and content.
- Approve shows the skill (first time) or a diff against the approved snapshot, asks `[y/N]` (default no), looks
  again after the prompt and refuses if anything changed, then records the row. A skill is usable only when approved
  AND its hash still matches.

## Two things the ADR got wrong, found while building (ADR amended)
1. **Where skills live.** The ADR said "inside the repository". Every file operation is contained to the workspace
   and the checkout is not inside it, so skills live in `<workspace>/skills/`.
2. **Where events are logged.** The ADR said `executions`, but every row there needs a task. A skill command has none,
   so there is an append-only `skill_events` table instead.
It also needed a `snapshot_json` column: a hash alone cannot produce the diff the ADR promises.

## Verified (Linux sandbox, Python 3.13)
`tests/test_skills.py`: 60 tests. Full suite: 588 passed, 1 skipped. Nine mutations were applied one at a time and
each made tests fail (no NUL separator 1, no LF normalisation 1, allow `allowed-tools` 3, skip the scripts refusal 6,
no link check 4, no re-check after the prompt 1, approve when declined 2, approval ignores the hash 2, name need not match the folder 1).

## Not verified
Windows: a real junction at `skills/` and the CLI on the ZBook. Junctions are simulated by patching `Path.is_junction`;
a real symlink test skips where symlinks are not allowed.

## Known limits, on purpose
Nothing uses an approved skill yet (no attach-to-task command). The frontmatter parser accepts a plain value that
contains `: ` (real YAML would not). `jarvis skill` has no create command: files are made with `write-file` or by hand.

## For Lone on the ZBook
Make `<workspace>/skills/demo-skill/SKILL.md` (see ADR-0006 for the shape), then run `jarvis skill list`, `show`,
`approve` (answer `n` once, then `y`), edit one character and `list` again (it must say CHANGED).

## Review and merge (added 2026-10-17)
Antigravity reviewed `f59ca02` on the ZBook (Windows 11, Python 3.14.8, SQLite 3.50.4). Verdict: merge, no findings.
It tested real `mklink /J` junctions at the skills folder, a skill folder, `references/`, a junction inside `references/`,
a dangling junction, and a junction swapped in after approval (all refused, `is_usable` false, `show` refuses). LF, CRLF and
lone CR gave one hash. It ran the real CLI end to end (list, show, approve `n` then `y`, edit one character gives CHANGED,
re-approve with diff, disable). Its 7 real-junction tests (`tests/test_skills_windows_verification.py`) skip off Windows.
Its own run: `tests/test_skills.py` 59 passed, 1 skipped (the real-symlink test: no Developer Mode).

Lone fast-forward merged `feature/antigravity-skills-v1-review` into `main` (`a26d018` to `b7f2963`) and pushed.
ZBook full run before the push: **583 passed, 13 skipped**. The Not verified section above is now closed for junctions
and the CLI. Still open: a real symlink check (needs Developer Mode). Skills are still not used by anything (see limits).
