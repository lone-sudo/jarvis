# 0010 — Fix: Multi-Line Clipboard Truncation + Classification Schema Validation

**Date:** 2026-09-23
**Contributors:** Lone (caught it live), Claude (diagnosed + fixed)

## Context
Lone ran `jarvis inbox-process` for real on the ZBook. Item 1's classification "succeeded" but
with empty tags and no visible reason why; item 2's response was truncated to a single `{`
character; item 3's leftover pasted JSON lines spilled directly onto the interactive PowerShell
prompt afterward, causing parser errors (`Unexpected token ':' in expression or statement`).

## Root cause
`ManualClipboardProvider.dispatch_prompt`'s confirmation step read:
> "Press Enter when the response is on your clipboard (**or ready to paste manually**)..."

This wording invited pasting the AI's response directly at that `input()` call — but `input()`
only ever reads one line. On Windows PowerShell, pasting multi-line text at an active `input()`
prompt causes each line to be delivered as a separate submission: the first line satisfies that
`input()` call, and the *remaining* lines stay queued at the console level, getting consumed by
whichever `input()` call comes next (the following item's classification prompt) — and once the
Python process exits with lines still queued, those leftover lines get delivered straight to the
interactive shell itself, which is exactly why PowerShell tried to parse `"tags": [...]` as a
command.

This wasn't a one-off — it's a structural flaw in the original prompt wording that would have
broken *any* multi-line paste at that exact point, for any user, on Windows.

## Fixed

1. **The confirmation prompt no longer invites pasting.** New wording: "Once the response is
   copied, press Enter (do NOT paste anything here)." The clipboard is always read via
   `pyperclip.paste()` after that single `input()` call — never from whatever was typed at the
   prompt.
2. **A visible preview of what was actually read**, printed immediately: `Read from clipboard (98
   chars): {"summary": "a b"...`. This makes a stale or wrong clipboard read immediately obvious,
   instead of surfacing only as a confusing JSON parse error two steps later.
3. **A genuinely safe multi-line manual fallback**, for when the clipboard is unavailable or reads
   empty: repeated `input()` calls (each one safely handles a single line) collected until an
   explicit `END` terminator line — not a single `input()` call that would truncate the same way
   the old flow did.
4. **Classification schema validation**, closing the second gap this exposed: item 1's response
   was valid JSON but a completely different shape (`title`/`source_url`/`category` instead of
   `summary`/`tags`/`actionable`/`related_project`/`confidence`), and the old code silently
   accepted it with defaulted (empty) fields and no warning. Now:
   - Fewer than 2 of the 5 expected keys present → rejected outright, item stays `UNPROCESSED`,
     clear error naming which keys were found vs. expected.
   - 2+ expected keys present but some missing → proceeds (a partial-but-real classification is
     still useful), but prints a loud `WARNING` naming exactly which fields were defaulted, so
     empty tags are visible and explained rather than silently mysterious.
   - Reproduced Lone's exact item-1 response verbatim as a test case, confirmed it now warns
     correctly instead of silently defaulting.

## Verified
- Direct reproduction: a mocked multi-line clipboard response confirmed `input()` is called
  exactly once regardless of how many lines the response contains, and the full content survives
  intact.
- Manual fallback confirmed to reconstruct multi-line content correctly via the terminator, not
  truncated the way the old single-`input()` fallback was.
- Lone's exact item-1 response (verbatim) reproduced as a test — confirmed it now triggers a
  clear `WARNING` instead of silently defaulting to empty tags.
- A completely unrelated JSON response (sharing zero expected keys) confirmed rejected outright,
  item correctly stays `UNPROCESSED` for retry.
- A fully correct response confirmed produces no warning at all.
- 125 tests total (118 previous + 7 new), all passing.

## Not yet done
Session/task consolidation (the next round Lone and the team were about to scope) is paused until
this fix is confirmed on the real ZBook — worth re-running the exact `inbox-process` sequence that
broke, for real, before trusting the fix the same way every other round in this project has
required real-machine confirmation before being called closed.
