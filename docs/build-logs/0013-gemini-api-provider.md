# 0013 — Gemini API Provider (V4, First Slice)

**Date:** 2026-09-27
**Contributors:** Lone (decision to proceed, delegated the --provider/--backend naming call to Claude), ChatGPT + Gemini (V4 proposal + guardrails), Claude (build + verify)

## Context
First slice of the "V4: Automated Intelligence Layer" proposal, scoped down to exactly what
the read-only assessment recommended as the smallest safe step: one new provider implementing
the existing `Provider` Protocol, tested entirely against mocked HTTP responses, zero real API
calls made during this round.

## Decision: new `--backend` flag, `--provider` untouched
The assessment found a real naming conflict before any code was written: `--provider` already
exists and means "label for manual dispatch" (e.g. `Claude_Web`), not "which implementation
handles this call." Reusing it for backend selection would have silently changed existing
behavior. Lone delegated this specific call ("do what you think is best"); resolved by adding
a separate `--backend` flag (`manual` default, `gemini` opt-in) so `--provider`'s existing
meaning and every existing invocation stays exactly as it was — confirmed by the full existing
test suite (146 tests) passing unchanged after the CLI edit.

## Endpoint verification, not assumed
Rather than build against a remembered/assumed API shape, searched Google's current
documentation directly before writing any adapter code. Confirmed: the `generateContent`
endpoint (`v1beta/models/{model}:generateContent`, `x-goog-api-key` header) is legacy but
explicitly still "fully supported" — the right fit here since Jarvis only needs single-turn
request/response, not the newer stateful Interactions API. Also found, via real developer
reports checked at write time, that some accounts see `404`/`403` even with valid keys and
confirmed quota — so the adapter's error messages name that possibility explicitly rather than
implying every 404 means "wrong URL."

## What was built
- `providers/gemini_api.py` (`GeminiAPIProvider`): stdlib `urllib.request` only, no SDK
  dependency. Implements the existing `Provider` Protocol with no changes to the Protocol
  itself. Handles: missing API key (clear message, no crash), HTTP 404/403/429 (each with a
  specific actionable hint), network/timeout errors, malformed JSON, and unexpected response
  shape — all returning a clear `ERROR: ...` string rather than raising, consistent with how
  `ManualClipboardProvider` already signals failure.
- `interface/cli.py`: `_select_backend(backend, provider_label)` — explicit selection only, no
  registry, no routing. `--backend` added to `ask-ai` and `inbox-process`, defaulting to
  `manual`.

## The known, named limitation — not hidden
`GeminiAPIProvider.dispatch_prompt` calls `PolicyValidator.authorize_provider(expects_cost=False)`
before every request — but that declaration is self-reported, not independently verified
against real Gemini billing state (this exact gap was flagged in the read-only assessment
before any code was written). This provider's $0 guarantee depends on Lone having confirmed
his own account's free-tier eligibility before ever using `--backend gemini` — the code cannot
verify that for him. Documented in the module docstring and covered by a test that confirms
the declaration is made, not that it's independently checked (because it isn't).

## Verified
- **API key never leaks into any error message** — tested directly with a real-looking fake
  key value, confirmed absent from every error path's output.
- **No automatic retry on 429** — confirmed via a call-counting mock that `urlopen` is invoked
  exactly once even when it raises a rate-limit error.
- **No automatic fallback to manual on gemini failure** — confirmed via a CLI-level test that
  `ManualClipboardProvider.dispatch_prompt` is never called when `--backend gemini` fails.
- **Default backend is a true no-op**: full existing 146-test suite passed unchanged after the
  CLI edit, before any new tests were added.
- **Live run with no API key set** (exactly the state on a fresh machine): clean, specific
  error message, no crash, task state correctly transitions through `AWAITING_USER` back to
  `IN_PROGRESS` even on failure — same pattern `ManualClipboardProvider` already used.
- 160 tests total (146 previous + 10 Gemini-provider + 4 backend-selection), all passing.

## Explicitly NOT built, per the converged V4 scope
Ollama provider (separate hardware-feasibility question, not a code question — Lone's to test
on the real ZBook, not assumed here). Provider/model registry, capability-based routing,
automatic fallback chains, background/scheduled calls. A real authenticated API call — this
round used mocked responses exclusively; the first real call with a genuine key is a deliberate
next step, not assumed to be part of this one.

## What still needs Lone
1. Confirm his own Google account's actual free-tier eligibility and current rate limits —
   not something this code or Claude's training data can verify.
2. Set `GEMINI_API_KEY` as a real environment variable (never in a file this repo tracks) and
   run one real `--backend gemini` call to confirm the endpoint/auth format holds up against
   the live API, not just the mocked tests.
3. Decide whether to pursue the Ollama slice next, or hold here.
