# ADR-0004: Telegram Interface — Read-Only, Foreground, Permanent Non-Goals

**Status:** Accepted
**Date:** 2026-09-28

## Context
Lone wanted phone-based access to Jarvis, inspired by azaris.ai (a paid, always-on, autonomous
cloud agent). The team agreed Azaris's *user experience* (mobile, conversational) was worth
exploring; its *operating model* (always-on, minimally-confirmed autonomous action, open-ended
browsing) was explicitly rejected as incompatible with Jarvis's $0 policy and human-control
principles — not deferred, permanently out of scope under the current architecture.

## Decisions
1. **Read-only first slice.** `/resume`, `/inbox`, `/help` only — `ActionTier.OBSERVE` exclusively,
   enforced at dispatch (`ALLOWED_TIERS`), not just by which commands happen to be registered.
   No writes, no fetches, no consequential actions.
2. **Confirmations deliberately NOT ported to Telegram.** Every `[y/N]` in Jarvis today is a
   blocking `input()` in the same process as the action — there is no existing mechanism for a
   confirmation to be issued, sent remotely, and safely redeemed later (bound to one exact
   action, single-use, expiring, non-replayable). Building that is real security design work,
   out of scope for this slice per Lone's explicit decision.
3. **Foreground only.** `jarvis telegram-start` runs until Ctrl+C. No daemon, no boot
   persistence, no scheduled restarts, no state written to disk between runs — the offline
   backlog is discarded on every startup rather than queued.
4. **Single authorized sender**, by numeric Telegram user ID (never a `@username`), private
   chats only. Anyone else receives no reply at all — not even an error — so a probing account
   can't determine whether a bot is listening.
5. **Fixed command table, no free-text passthrough.** Message text is never handed to argparse,
   a shell, or `input()`. `input()` is explicitly disabled during command execution so a command
   that tried to prompt for confirmation fails loudly instead of hanging the bot.
6. **Token handling**: `TELEGRAM_BOT_TOKEN` from environment only, same pattern as
   `GEMINI_API_KEY`. Scrubbed from every error/log line via two independent mechanisms (exact
   string match, and a regex for the token's general shape) since the token lives inside the
   request URL itself.
7. **Privacy**: Lone explicitly accepted that read-only status/listing content passing through
   Telegram's infrastructure is acceptable for this slice (decision made 2026-09-27). This is
   the first feature where Jarvis sends more than a bare URL through a third party.
8. **Permanent non-goals**, agreed by Lone + ChatGPT + Gemini: always-on background execution,
   minimally-confirmed autonomous actions, open-ended web browsing/crawling. Reconsidering any
   of these requires an explicit new ADR, not an incidental consequence of adding an interface.

## Consequences
- A remote confirmation mechanism, if ever built, is a separate, deliberately-scoped future ADR
  — not an incremental extension of this one.
- The bot only runs while Lone is actively running it; messages sent while it's stopped are
  silently lost, by design.
- Stdlib-only (`urllib.request`), consistent with the dependency discipline established when
  `trafilatura` was rejected and the Gemini provider was built stdlib-first.
