# 0014 — Read-Only Telegram Interface

**Date:** 2026-09-28
**Contributors:** Lone (privacy/scope/dependency decisions), ChatGPT + Gemini (design review), Claude (build + verify)

## Context
Lone found azaris.ai and asked how close Jarvis could get to it at $0. Team agreed: chase the
*experience* (mobile, conversational), reject the *mechanism* (always-on, minimally-confirmed
autonomous action, open-ended browsing) as permanently incompatible with Jarvis's principles —
see ADR-0004. Lone made three decisions: (1) read-only content passing through Telegram's
infrastructure is acceptable, (2) read-only first slice, no confirmations, (3) stdlib-only.

## What was built
- `interface/telegram_bot.py`: `TelegramClient` (stdlib `urllib.request`, `getUpdates`/
  `sendMessage` only), a fixed read-only command table (`/resume`, `/inbox`, `/help`, `/start`),
  `handle_update` (allowlist + dispatch), `run_bot` (foreground long-polling loop).
- `cli.py::cmd_telegram_start` / `jarvis telegram-start` subcommand.
- `ADR-0004`: the permanent non-goals and this slice's boundaries, formally recorded.

## Verified — with an unusual amount of rigor for this round, deliberately
This is the first feature that talks to a third party carrying a secret token and gates who can
reach Jarvis remotely, so beyond the normal test pass, **every security-critical guard was
mutation-tested**: the corresponding protection was deliberately broken in the source, and the
test suite re-run to confirm a real failure resulted, not just a passing suite.

**First mutation pass — 4 of 9 guards were NOT actually caught by their tests**, despite the
tests passing normally:
1. Removing the `input()`-blocking guard: not caught. The original test asked for a real
   terminal input during a pytest run, which pytest's own stdin handling fails on regardless of
   whether Jarvis's guard exists — the test was passing for the wrong reason. Rewritten to give
   the handler a working fake `input()` that would visibly succeed (returning `"y"`) if the
   guard were missing; now genuinely proves the guard's effect.
2. Disabling token redaction: not caught. The test's fake Telegram server never echoed the token
   back in error text, so a broken redaction function had nothing to fail against. Rewritten so
   the fake server includes the real token in its error response, forcing genuine redaction to
   occur for the test to pass.
3. Removing `from None` on a raised exception (which suppresses the exception chain so request
   details can't leak via a traceback): not caught — nothing was asserting on `__cause__`/
   `__suppress_context__`. Added explicit assertions on both across every client error path.
4. Advancing the offset after handling instead of before (reopening a replay window on failure):
   not caught — no test exercised the actual failure mode (a reply that fails to send, or an
   unexpected exception inside `handle_update`). Added both as explicit scenarios, and hardened
   the loop itself to catch bare `Exception` around `handle_update` (previously only
   `TelegramAPIError` was caught) plus skip malformed updates with no `update_id` instead of
   crashing on them — both real gaps the missing test coverage had hidden.

After fixes, **re-ran the same 9 mutations: all caught.** 46 tests total (39 initial + 7 added
during hardening), all passing against the corrected code.

## Additional verification
- **Full live-fire round trip over real HTTP sockets** (a local server standing in for
  Telegram's actual protocol): registered a real project/task, sent `/help` and `/resume` as the
  authorized owner — both replies correct, `/resume` showing the real task — and a message from
  an unauthorized sender in the same run, confirmed zero reply sent and only the sender ID (not
  the message text) logged locally.
- Full existing suite (206 tests) passes unchanged — nothing about this addition touched
  existing behavior.

## Honest scope limits — this needs Lone's real Telegram account and ZBook, not sandbox
This sandbox cannot create a real Telegram bot or receive a real message. Confirmed here: the
client's request shapes, the allowlist/dispatch/redaction logic, and the polling loop's failure
handling — all against a faithful local stand-in. **Not yet confirmed**: that Telegram's actual
service accepts these exact request bodies (the `link_preview_options` field in particular —
built from documentation, not a live response), real token issuance via BotFather, and finding
a real numeric user ID. These need Lone to actually set up a bot and run one live conversation
before this is considered fully verified, same discipline as every prior real-network feature
(V3's fetch, the Gemini provider).

## What Lone needs to do next (steps to follow separately)
1. Create a bot via @BotFather, get a token.
2. Find his own numeric Telegram user ID.
3. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_ID` as environment variables for one
   PowerShell session (never saved to a file this repo tracks).
4. Run `jarvis telegram-start`, message the bot from his phone, try `/help`, `/resume`, `/inbox`.
5. Confirm Ctrl+C stops it cleanly and a restart doesn't replay old messages.
