# 0012 — Stdlib HTML Extraction + Provider Protocol Generalization

**Date:** 2026-09-26
**Contributors:** Gemini (extraction proposal), ChatGPT (dependency review sequencing + provider scope), Claude (build + verify)

## Context
Two small, narrowly-scoped items converged across all three parties this round:
1. Raw HTML fetched by V3 needed cleaning before it reaches the clipboard/classification loop
   (Gemini's original observation — otherwise the human-in-the-loop AI dispatch wastes context
   on navigation links, script tags, CSS).
2. A second provider is genuinely on the horizon (Gemini API, eventually Ollama), which per
   Claude's earlier read-only assessment is the actual trigger for generalizing the provider
   interface — not before that point.

## Dependency decision: trafilatura rejected, stdlib chosen
Verified directly (not taken on faith): `pip show trafilatura` confirms `Requires: certifi,
charset_normalizer, courlan, htmldate, justext, lxml, urllib3` — six real dependencies, including
`lxml` (a C-extension library). License is Apache 2.0 (fine), but the dependency weight
contradicts the project's lean-local-ZBook constraint for what is fundamentally a "strip
boilerplate tags" job. ChatGPT's independent review reached the same conclusion. Rejected;
stdlib `html.parser` chosen instead.

**Honest limitation, stated plainly:** this sandbox's `web_fetch` tool always pre-extracts
content — there is no way to obtain genuinely raw HTML here to run a fair raw-in/clean-out
comparison against trafilatura. The dependency decision itself didn't need that comparison
(the dependency tree alone was disqualifying), but *validating extraction quality* against real
content requires Lone's actual captured item (`INB-3A322D98`, the real Wikipedia fetch from V3) —
provided as `compare_extraction.py` for him to run and report back, rather than fabricated with
synthetic sandbox HTML.

## What was built
- `tools/text_extraction.py`: `extract_readable_text(html) -> str`. Strips `<script>`, `<style>`,
  `<nav>`, `<header>`, `<footer>`, `<noscript>`, `<svg>`, `<form>`, `<button>`, `<aside>` and their
  entire contents; preserves paragraph breaks at block-level tags. Zero third-party dependencies.
  Deliberately lower extraction quality than trafilatura's heuristics (no "is this the main
  article" scoring) — an accepted tradeoff per the converged review.
- Wired into `cmd_save`'s `--fetch` path: `text/html` responses are extracted before storage;
  `text/plain` passes through unchanged. Nothing else in the fetch/policy/inbox pipeline touched.
- `providers/base.py`: a `runtime_checkable` `Provider` Protocol (`dispatch_prompt(str) -> str`,
  one method, nothing else). `dispatch_and_track` generalized into a standalone function
  accepting any conforming `Provider`, not hardcoded to `ManualClipboardProvider`. The existing
  method-style call (`provider.dispatch_and_track(...)`, used by `cmd_ask_ai`) is preserved as a
  thin wrapper — zero behavior change for existing call sites.
- Explicitly NOT built, per the converged scope: no registry, no routing, no fallback chains, no
  local-model manager, no actual second provider implementation.

## A small real bug caught immediately
`isinstance(provider, Provider)` raised `TypeError: Instance and class checks can only be used
with @runtime_checkable protocols` on the first test run — `typing.Protocol` requires the
`@runtime_checkable` decorator before `isinstance` works against it. One-line fix, caught by
actually running the test rather than assuming the Protocol would just work.

## Verified
- **Extraction, against representative boilerplate patterns**: script/style content excluded,
  nav/header/footer/aside excluded (including a sidebar "ad" block), real article paragraphs
  preserved, malformed/unclosed HTML tolerated without raising, empty input handled.
- **Full CLI flow, live**: a mocked fetch returning HTML with script/nav/footer confirmed that
  only the real article text (`Real Title`, `Real article content about databases.`) landed in
  the actual inbox Markdown file — script/nav/footer content never reached storage.
- **Protocol conformance**: both `ManualClipboardProvider` and a minimal fake test double satisfy
  `Provider` structurally (no shared base class needed).
- **The actual generalization point**: `dispatch_and_track` (standalone function) called with the
  fake provider — proves the state-transition wrapping works with *any* conforming object, not
  just the concrete clipboard class.
- **Backward compatibility**: the existing `provider.dispatch_and_track(...)` method call (used
  by `cmd_ask_ai`) still works unchanged, confirmed via the same task-status-transition test used
  originally in V1.
- 146 tests total (137 previous + 9 new), all passing.

## What still needs Lone's real machine
Extraction quality against **real** raw HTML (not synthetic sandbox samples) — run
`compare_extraction.py` against the real `INB-3A322D98` item and report back whether the
extracted text is clean or has leftover boilerplate the stdlib approach missed. Sandbox
verification here is necessarily partial, same honest distinction held throughout this project.
