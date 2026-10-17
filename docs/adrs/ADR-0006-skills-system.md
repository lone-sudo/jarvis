# ADR-0006: Skills System — Text-Only, Hash-Pinned, Owned by Jarvis

**Status:** Accepted by Lone on 2026-10-17 (reviewed by Antigravity; merged to `main` as a proposal first). Version 1 is built on `feature/claude-skills-v1` (build log 0024) and awaits review. Nothing here is verified on the ZBook until Lone runs it (invariant 5).
**Date:** 2026-10-16

## Context
Build log 0018 evaluated ten external tools. Several ship "skills": folders with a `SKILL.md` that tell
an AI how to do one kind of task. Lone wants a skills system for Jarvis, and a first useful workflow on
top of it (a saved link becomes a reviewed note). He approved on 2026-10-16: build the skills system
text-only first, write this ADR before any code, and do not adopt the ten tools as runtimes.

Skills are third-party-shaped text that steers an AI. AGENTS.md already says anything that "routes AI
traffic, holds keys, or runs code needs review first". This ADR keeps skills on the safe side of that
line: they are reviewed text, they hold no keys, and in version 1 they run nothing.

## Decision
1. **A skill is a folder** `<workspace>/skills/<name>/` (the workspace root, `JARVIS_WORKSPACE_ROOT`), holding
   `SKILL.md` and optional flat `references/*.md`. *Amended during the build:* this said "inside the repository",
   but every Jarvis file operation is contained to the workspace (invariant 2) and the Jarvis checkout is not
   inside it. Skills therefore live in the workspace. Making that folder a git repository, so that every change is a
   diff another model reviews, is up to Lone and does not change the code.
2. **Version 1 is text only.** A skill can be listed, shown and approved. It cannot execute code, call a
   tool, fetch a URL, or grant itself any permission. Tools stay governed by the policy layer and the
   engine's four-tool allowlist (ADR-0005 g).
3. **Strict subset of the Agent Skills format.** Accepted frontmatter keys: `name`, `description`,
   `license`, `compatibility`, `metadata`. A skill that carries any other key (notably `allowed-tools`) or
   a `scripts/` or `assets/` folder is **refused with a clear message**, never silently ignored.
   `name` must equal the folder name. Size limits apply to `SKILL.md` and to the whole folder.
   *Checked against the published specification (agentskills.io/specification, read 2026-10-17):*
   - `name` and `description` are required. `name`: 1 to 64 characters, lowercase letters, digits and
     hyphens, no leading or trailing hyphen, no `--`, must match the parent folder. The spec allows
     Unicode lowercase letters; Jarvis is stricter and accepts ASCII `a-z`, `0-9` and `-` only.
   - `description`: 1 to 1024 characters, non-empty.
   - `license` (optional, short). `compatibility` (optional, 1 to 500 characters).
     `metadata` (optional, a map of string keys to string values).
   - `allowed-tools` is optional and marked experimental in the spec. Jarvis refuses it.
   - The spec lets a skill folder hold any extra files. Jarvis is deliberately stricter: only `SKILL.md`
     and `references/*.md`. The spec's own reference validator (`skills-ref validate`) was not run.
   - The spec recommends `SKILL.md` under 500 lines and about 5,000 tokens. Jarvis sets a hard size cap.
4. **Hash pinning.** A skill's identity is the SHA-256 of its folder: for each file in sorted
   POSIX-relative-path order, the path, a separator and the file's content, where content is read as UTF-8
   text with `\r\n` and lone `\r` converted to `\n` first. Without that, git's `core.autocrlf` would give
   the ZBook (CRLF) and the sandbox (LF) different hashes for the same skill (Antigravity's review).
   A file that is not valid UTF-8, or contains a NUL byte, is refused. Between a path and its content, and
   between files, the hasher writes a NUL byte: no path or accepted content can contain one, so two different
   folders cannot produce the same byte stream (Antigravity's re-review). Approval records that hash. Any change to any file makes the skill `unapproved` until it is approved
   again. An unapproved skill can be shown (with a warning) but is never reported as usable.
5. **State in SQLite, content in Markdown** (invariant 4). A `skills` table holds: `name` (primary key),
   `path` (workspace-relative, POSIX style), `content_hash`, `status` (`approved` or `disabled`; "proposed" is simply
   a skill with no row), `approved_at`, and `snapshot_json` (the approved text, so a later approval can show a real
   diff; a hash alone cannot). Migration 007 adds it, together with an append-only `skill_events` table (see 9). Its number is the next free one on `main` at build time (006 is
   taken by the run-lock branch, so if that is not merged yet the skills branch must be cut after it, or take
   the next number and say so in its build log). Two branches must never both add the same number.
6. **Commands.**
   - `jarvis skill list`: names, status, and whether the folder hash still matches.
   - `jarvis skill show NAME`: prints the skill; read-only.
   - `jarvis skill approve NAME`: shows the hash and a diff against the last approved version, then asks
     `[y/N]`. Declining changes nothing. Approving writes only the SQLite row.
   - `jarvis skill disable NAME`: sets `disabled`. Archive, never delete (existing project rule).
   Creating or editing a skill file goes through `FileSystemInspector.write_file` (diff and `[y/N]`,
   invariant 3). The skill code must never write a skill file any other way.
7. **Containment.** Every path goes through `SecurityPolicy.resolve_safe_path()`. Discovery, hashing and
   `show` all refuse a link or junction anywhere: the `skills/` folder itself, a skill folder, and any file
   or folder inside one. Reuse the shared `_is_link` check (it needs Python 3.12 on Windows, see Related
   decisions) and check the path itself as well as its parents, the way the plan-commit guard now does
   (build log 0022, review finding 1). Otherwise a junction could smuggle an outside tree into the registry.
8. **Skill text is data.** It is never injected into a prompt automatically and no hook fires on it. A
   person or an AI attaches a skill to a task spec on purpose. Text from a skill never changes policy.
9. **Logging.** Every `list`, `show`, `approve` (including declined and refused) and `disable` is appended to
   `skill_events` with the skill name and hash. *Amended during the build:* this said `executions`, but every
   `executions` row needs a task (`task_id` is a required foreign key) and a skill command is not part of a task.
   A separate event table keeps the audit trail without forcing a fake task. Events are never updated or deleted.

## What this deliberately leaves out
Auto-discovery or search of remote skill registries (Find Skills), a marketplace, telemetry, self-modifying
skills (Task Observer), script execution, MCP servers, and always-on hooks (Ponytail). Each needs its own ADR.

## Consequences and costs
- Community skills (for example from addyosmani/agent-skills) can be copied in if they pass the subset
  rules, but each still needs a review and an `approve`. That is friction by design.
- A skill cannot yet run a check or a script. Anything that needs code waits for an engine step type.
- A one-byte edit unapproves a skill. That is the point, and it will occasionally annoy.
- Hash pinning proves a skill did not change since approval. It does not prove the skill is wise. Review does that.

## Acceptance for version 1 (all must pass, then Lone runs them on the ZBook)
- A valid text skill lists, shows and approves. Declining the prompt leaves the row unchanged.
- A skill with `allowed-tools`, an unknown frontmatter key, `scripts/` or `assets/` is refused (one test each).
- Editing an approved skill by one byte marks it unapproved (test).
- A skill name or path that escapes `skills/`, or a link or junction at the `skills/` folder, a skill
  folder, or any file or folder inside one, is refused (the path itself, not only its parents).
- The same skill with LF and with CRLF line endings has the same hash (test).
- No skill file is written without the diff and `[y/N]`.
- At least 20 new tests. The exact pass count is reported, with where it ran (Linux sandbox or Windows).

## Related decisions (approved by Lone 2026-10-16, built as separate changes)
- **Raise the Python floor from 3.10 to 3.12.** `Path.is_junction` needs 3.12, and the link guards on
  rollback and on a plan's `git_commit` cannot see Windows junctions on older versions. The ZBook runs 3.14.
  This is its own small change to `pyproject.toml` and the docs, reviewed separately.
- **The saved-link workflow** (inbox record, V3 fetch of public pages, a drafted note behind `[y/N]`) is the
  first user of this system. It gets its own ADR or ADR-0006 amendment once version 1 exists.
- **The logged-in browser approach stays out of scope.** The V3 "no cookies, no crawling" safeguard and the
  non-goal "no custom scrapers for TikTok or Instagram" are unchanged.

## Non-goals
No remote skill installs, no auto-update, no skill that can write files or run commands, no telemetry.
