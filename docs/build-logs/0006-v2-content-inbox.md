# 0006 — V2: Content Inbox

**Date:** 2026-09-21
**Contributors:** ChatGPT, Gemini (architecture review), Lone (decisions), Claude (build + verify)

## Context
First V2 feature per the frozen roadmap: manual save → inbox → classify → optional
project connection. Both ChatGPT and Gemini reviewed and converged on nearly all of
the design; two points were escalated to Lone.

## Decisions made this round
- **Auto-fetch from saved URLs: deferred, not rejected.** Lone's framing: "if this is
  planned for a future version, do it manually now." Claude's answer: yes, plausible
  V3+ candidate, but it needs its own policy-tier design first (allowed domains, size/
  time limits, a new "Jarvis makes an outbound request" capability that doesn't exist
  yet) — so V2 stays fully manual, consistent with the existing ManualClipboardProvider
  pattern. `jarvis save --url` stores the URL as a bare reference; Jarvis never fetches it.
- **Storage folder: `inbox/`** (Lone's preference, matching Gemini's proposal over
  ChatGPT's `knowledge/`/`reference/`).

## A design conflict caught before building, not after
Gemini's schema used a single `project_key` column for both the confirmed link AND
implicitly for whatever the classifier suggested. That would mean classification alone
could auto-link an item to a project — directly contradicting the "classification
suggests, never forces" principle both reviewers insisted on elsewhere in the same
message. Fixed before writing any code: split into `project_key` (confirmed link, set
ONLY by `jarvis inbox-link`, has a real FK to `projects`) and `suggested_project_key`
(the classifier's guess, advisory only, unconstrained since it's just AI output).

## What was built
- **Schema:** `inbox_items` table — `UNPROCESSED` / `PROCESSED` / `ARCHIVED` lifecycle
  (LINKED/UNLINKED collapsed into whether `project_key` is null, per both reviews).
- **`state/inbox.py` (`InboxStore`):** create/get/list/mark_processed/link_to_project/archive.
- **`memory/inbox_markdown.py`:** writes `inbox/<id>.md` with YAML frontmatter + Original
  Capture section at save time; `write_classification` adds an AI Summary section
  *without ever touching* the Original Capture text — reversible classification, tested
  directly (see below).
- **`memory/inbox_prompt.py`:** the strict JSON classification prompt, explicitly
  instructing the AI not to suggest task creation (defense in depth alongside the
  CLI-side gate) and to return `null` for `related_project` rather than guess.
- **CLI:** `jarvis save`, `jarvis inbox-process`, `jarvis inbox`, `jarvis inbox-link`,
  `jarvis inbox-archive`.
- **Classification is NOT a silent background call.** `inbox-process` walks
  UNPROCESSED items through the same `ManualClipboardProvider`/paste-back mechanism
  `ask-ai` already uses — one human-in-the-loop round trip per item. This follows
  directly from the $0-spend policy; there is no other provider to call silently.

## Verified (not just implemented)
- Full save → classify → list flow, run for real with two items (one URL reference,
  one raw-text capture).
- **Reversible classification, proven not assumed:** wrote raw capture, read it back,
  classified, read it back again — byte-identical both times.
- **Malformed AI response handled safely:** simulated a non-JSON paste-back; the item
  correctly stayed `UNPROCESSED` for retry rather than corrupting state or crashing.
- **Anti-overload gate, proven with a live run:** after classifying an item as
  `actionable: true` with a `related_project` suggestion, confirmed directly against
  the database that `project_key` was still `NULL` and no task was created — only
  `jarvis inbox-link`, called explicitly, set it.
- **Hallucinated project key rejected:** simulated a classifier response naming a
  project that was never registered; confirmed it was discarded (`suggested_project_key`
  left `NULL`) rather than trusted.
- **FK constraint on `project_key` (not `suggested_project_key`) holds:** a raw
  attempt to set `project_key` to an unregistered project correctly fails.
- 50 automated tests pass (37 previous + 13 new in `tests/test_inbox.py`).

## A real (minor) gap found during testing, not a code bug
Hit a stale-database error mid-testing: an existing `jarvis_state.db` from earlier in
this same session didn't have the new `inbox_items` table's latest column shape,
because `CREATE TABLE IF NOT EXISTS` doesn't add columns to a table that already
exists with an older shape. For this release that's harmless — `inbox_items` is an
entirely new table, so real users upgrading from v1.3 will have it created cleanly
the first time they run any command. But it's worth naming honestly: **Jarvis has no
schema migration system yet.** The first time a future version needs to change an
*existing* table's columns (not just add a new table), this will need real handling —
flagged here rather than left as a surprise later.

## Not built (per both reviews' explicit scope boundary)
Embeddings/vector search, auto-fetch/scraping, browser automation, a knowledge graph,
autonomous task creation, learned routing — all still correctly out of scope.
