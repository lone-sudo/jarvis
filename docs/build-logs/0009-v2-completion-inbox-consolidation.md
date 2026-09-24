# 0009 — V2 Completion: Inbox Consolidation

**Date:** 2026-09-22
**Contributors:** ChatGPT, Gemini (design + refinement), Claude (build + verify)

## Context
Deduplication/clustering and consolidation were explicitly in V2's original frozen scope but never
built — only the save/classify/link pipeline was. Closing that out now, per Lone's direction to
keep building rather than pause, before moving to any genuinely new capability. Still no embeddings,
per the standing boundary — this is Jaccard similarity over tag sets, purely keyword-based.

## Design evolution across the review round
Claude's initial proposal used a fixed "≥2 shared tags" threshold. Gemini correctly identified this
as fragile (2 shared tags out of 2 total is identical; 2 shared out of 20 total is barely related)
and proposed Jaccard similarity (intersection/union) with a proportional threshold instead — adopted
at ≥0.5. ChatGPT agreed and added two requirements: an explicit, documented rule for transitive
clusters (A~B and B~C pass but A~C doesn't — do they merge?), and a failure-atomicity invariant
(a failed write must never result in archived-but-lost originals). Gemini separately and
independently strengthened the project-boundary rule: `suggested_project_key` must never grant
write access to a project's Markdown memory — only a confirmed `project_key`, set exclusively via
`jarvis inbox-link`, may result in a write to `ProjectMemory`. All three converged; implemented
exactly as agreed.

## What was built
- `memory/consolidation.py` — pure clustering functions, no I/O:
  - `jaccard_similarity`, `normalize_tags` (case/whitespace normalized, per ChatGPT's caveat)
  - `find_clusters`: buckets items by **confirmed** `project_key` first (None bucket = everything
    not confirmed-linked, regardless of any suggested key), then clusters within each bucket via
    connected-components (union-find) over pairwise Jaccard ≥ 0.5 — the transitive-clustering rule
    ChatGPT asked to have made explicit and deterministic.
  - `build_consolidated_note`: human-readable synthesis (shared tags, source IDs, combined
    per-item summaries).
- `interface/cli.py::cmd_inbox_consolidate` — the full preview-first flow: discover clusters,
  print a preview (project, shared tags, item list, proposed note text) per cluster, `[y/N]`
  confirm, then write-then-archive with atomicity.

## The structural guarantee, not just a runtime check
Bucketing by confirmed `project_key` *before* clustering means two items confirmed-linked to
different projects can never end up in the same cluster in the first place — proven directly:
two items with **identical tags** but different confirmed `project_key`s produce zero clusters.
This is stronger than a runtime "don't write to two projects" check, which the design deliberately
avoided needing.

## A real bug found and fixed during this round, unrelated to the new feature
While testing consolidation output for real, caught a pre-existing bug in `memory/markdown.py`
(`ProjectMemory.append_note`, present since V1): its heading-prefix logic was inverted — it added
a `## ` prefix *when the note already started with `#`*, producing `## ## Consolidated: ...` in
real output. Fixed: a note already formatted as a heading by its caller gets no prefix added;
only plain-text notes get one. Caught by actually reading the generated Markdown file, not by
reviewing the code.

## Verified — with real scenarios, not just assertions
- **Jaccard math**: identical sets → 1.0; small overlap in large sets → low score, correctly *not*
  clustered despite 2 shared tags (proving proportionality actually matters, not just raw count).
- **Transitive clustering, constructed precisely**: A={x,y,z}, B={y,z,w}, C={z,w,v} — A~B=0.5,
  B~C=0.5, A~C=0.2 (fails alone) — confirmed all three merge into one cluster via the connected-
  components rule, and confirmed the result is identical regardless of input order (determinism).
- **The core structural guarantee**: identical-tag items with different confirmed `project_key`s
  produce zero clusters; identical-tag *unlinked* items correctly do cluster.
- **Full CLI flow, live**: three real items (two related + one unrelated) → correct 1-cluster
  result → confirmed → note written to the correct project's `JARVIS_NOTES.md` → originals
  `ARCHIVED` (confirmed still readable, not deleted) → unrelated item untouched.
- **Atomicity, the invariant ChatGPT specifically required**: simulated a write failure
  (`ProjectMemory.append_note` raising) during a confirmed consolidation — confirmed both source
  items remained `PROCESSED`, neither was archived. Nothing disappears on failure.
- **`suggested_project_key` never grants write access**: two unlinked items with a suggested
  project consolidated successfully, but confirmed directly that no file was ever written under
  that project's directory — the merge instead produced a new plain inbox item, exactly as Gemini's
  correction required.
- **Decline path**: confirming "n" leaves both source items exactly as they were.
- 118 tests total (104 previous + 14 new), all passing.

## Not built (per the converged, still-frozen boundaries)
No embeddings/semantic similarity. No scheduler — "nightly" stays deferred, on-demand only, per
all three parties' explicit agreement not to introduce unattended execution here. No automatic
triggering from `inbox-process` — consolidation remains a separate, deliberate human decision.
