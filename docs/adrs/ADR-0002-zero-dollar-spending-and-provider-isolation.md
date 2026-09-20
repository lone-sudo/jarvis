# ADR-0002: $0 Automatic Spending, Provider Isolation

**Status:** Accepted
**Date:** 2026-09-20

## Context
Prevent unexpected cloud charges; keep AI providers replaceable rather than central.

## Decision
- Hard policy constant `MAX_AUTO_SPEND_USD = 0.00` in `policy/rules.py`.
- `PolicyValidator.authorize_provider` blocks any provider call flagged `expects_cost=True`.
- Milestone 001 ships one provider adapter: `ManualClipboardProvider` — copies a prompt to
  clipboard (or prints it, if no clipboard is available), waits for the user to paste it
  into their existing ChatGPT/Claude/Gemini subscription, and reads the response back.
  No API keys, no billing.

## Consequences
- Zero financial risk in Milestone 001.
- Interaction loop is manual/slower during early phases — accepted tradeoff.
- Task status enum includes `AWAITING_USER` specifically to represent a task paused on
  a human pasting a response back — without this, the state machine has no way to
  represent "waiting on the human," a gap identified during implementation review.
