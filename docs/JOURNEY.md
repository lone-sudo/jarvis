# The Jarvis Journey

A narrative account of building Jarvis, from first idea through the current state. Pulled
together from 15 build-logs and 4 ADRs — those remain the detailed record; this is the story
they tell when read in sequence. Written per the original project brief's request for a genuine
engineering case study, not a polished retrospective: failures are kept in, not cleaned up.

## The idea

Lone wanted something more than another chatbot — an orchestration layer that owns state,
memory, task continuity, and tool execution, while treating AI models (ChatGPT, Claude, Gemini,
and eventually local models) as replaceable workers rather than the center of the system. The
non-negotiable constraint from day one: **$0 automatic spending**. No API billing, no
pay-as-you-go, no silent fallback to a paid tier. Everything would run on subscriptions Lone
already had, accessed manually, or stay local.

The team was explicit from the start: ChatGPT and Gemini as architecture reviewers, Claude as
the sole coding agent, Lone as final decision-maker. A governance rule settled disagreements —
unanimous agreement needed no sign-off, a 2-1 split went to Lone with the dissent explained, a
three-way split was Lone's call outright. This rule got used for real more than once, not just
stated as a formality.

## Milestone 001 — proving the brain works without any AI

The first build wasn't an AI feature at all — it was proving Jarvis could track its own state
(sessions, tasks) independently of any model, survive a crash, and recover. Gemini wrote the
first draft of the code; Claude implemented and tested it for real, which immediately surfaced
six bugs invisible from reading the code alone: SQLite's foreign-key enforcement silently
disabled after the first connection, a database path that depended on which folder you launched
from, no real link between a project's name and its actual directory, an uncaught crash if `git`
wasn't installed, a clipboard library that could crash the whole CLI on import, and a CLI that
could only show status with no way to actually create anything. All fixed, all tested, delivered
as a working zip rather than code snippets — a pattern that held for the rest of the project.

## V1 — confirmed writes and the first real AI loop

Added the ability to actually change things: writing files and committing to git, both gated
behind a diff preview and an explicit `[y/N]`. A declined action gets logged as
`REJECTED_BY_POLICY`, not silently dropped — the audit trail matters as much as the gate itself.
Alongside it, the first AI dispatch loop: copy a prompt to the clipboard, paste the response
back, with the task's state correctly tracking `AWAITING_USER` the whole time it's waiting on a
human. No API call anywhere in this loop — by design.

## Gemini's GitHub review — the first real security catch

Once the repo was live, Gemini reviewed it directly on GitHub and found five real gaps: a
`--content-file` argument that read an arbitrary local file *before* any policy check ran (a
genuine path-traversal risk, confirmed with a live-fire test against `/etc/passwd`), no boundary
check on project registration, a Markdown-writing module with no path check at all, task status
transitions that only checked "is this a known status" rather than "is this a sane transition,"
and the repository itself set to public instead of private. All five fixed and verified. Then
ChatGPT and Gemini jointly pushed back on keeping the policy layer boolean-based rather than
exception-based — a 2-1 majority Claude had resisted unilaterally, settled properly through the
governance rule once it came from two independent reviewers rather than one.

## The Windows regression — and the lesson that shaped everything after

The fix for Windows drive-letter paths (`C:\...`) was tested and passed in Claude's Linux
sandbox. The first time Lone ran it on the actual ZBook, it broke — the "fix" rejected every
legitimate path in the real workspace, because the sandbox had no way to exercise real Windows
path semantics. This became the project's defining discipline: **sandbox-verified is not the
same as real-machine-verified**, stated explicitly in every build-log from this point forward,
and the reason nothing was ever called "done" without Lone's own test run.

## V2 — a content inbox that refuses to overwhelm

The content inbox (`jarvis save`, `jarvis inbox-process`, `jarvis inbox-link`) was built around
one hard rule: saving something, or classifying it, must never silently create a task or link a
project. An AI's classification is a *suggestion*; only an explicit `jarvis inbox-link` creates a
real connection. This showed up as a real schema decision — `suggested_project_key` and
`project_key` as two separate columns, not one — specifically so an AI's guess could never
quietly become a confirmed link.

## Two moments the team had to self-correct in public

**The content-file bug, round two.** Gemini reviewed the live repo again after V1 hardening and
found the exact same class of bug ChatGPT had separately flagged — Claude fixed it, verified it,
and moved on, treating two independent catches of the same root issue as confirmation rather than
redundancy.

**The build-log that documented its own mistake.** Build-log 0004 switched the policy layer to
exception-based handling and closed what looked like the Windows path gap with a regex. Build-log
0005 exists specifically to document that the regex was wrong — it rejected *every* Windows path
unconditionally, which would have broken the tool on the one platform it actually needs to run on.
The team's explicit choice was to keep 0004 as written, mistake included, rather than edit history
to look cleaner. That's a deliberate decision about what this documentation is for.

## The migration system — built because of a gap, and it immediately caught itself failing

V2 shipped with a documented gap: no way to safely change an existing table's shape. The
migration system built to close it immediately validated its own necessity: testing
`sqlite3.executescript()`'s transaction behavior (rather than assuming it) revealed it does *not*
respect an enclosing manual transaction — a failing multi-statement script left an earlier
statement's effect committed anyway. Had that shipped unexamined, it would have silently broken
the exact atomicity guarantee the whole system existed to provide.

## V3 — the first capability that leaves the machine

Controlled URL fetching needed real threat-modeling: SSRF, DNS rebinding, redirect validation,
byte-capped streaming. ChatGPT's review was notably thorough here — IP-literal bypasses, IPv4-mapped
IPv6 addresses, CGNAT ranges, proxy-environment interference, credential-bearing URLs. Building it
surfaced two real bugs: a TLS connection that would have used the resolved IP instead of the real
hostname for certificate verification (breaking real fetches outright, not just weakening them),
and relative redirect headers that weren't resolved before re-validation — caught only because the
tests ran against a real local HTTP server instead of a mock. The eventual real-world proof: a live
fetch against Wikipedia, genuine DNS resolution, a real TLS handshake, and a non-allowlisted domain
correctly blocked in the same session.

## Closing V2's deferred scope — and two more design corrections mid-stream

Inbox consolidation (clustering similar saved items) started as a fixed "≥2 shared tags"
threshold; Gemini correctly identified this as fragile and proposed Jaccard similarity instead,
adopted at a 0.5 threshold. Session consolidation surfaced a genuine 2-1 split — Gemini wanted
`project_key` added directly to sessions; ChatGPT and Claude preferred deriving ownership from a
session's tasks at consolidation time, since every task already carries a mandatory project key.
Lone's governance rule settled it in ChatGPT and Claude's favor. Verifying the new migration this
added broke two *existing* migration tests — investigated rather than silenced, revealing the
migration runner had no protection against two files claiming the same number, a real landmine
for any future contributor.

## The bug that broke in Lone's own terminal

Running `inbox-process` for real, Lone hit a cascading failure: a classification silently
defaulted to empty tags with no warning, a response got truncated to a single `{` character, and
leftover pasted text spilled directly onto his live PowerShell prompt, throwing parser errors.
The root cause was a single ambiguous sentence in a confirmation prompt — inviting the user to
paste a multi-line response at a prompt that could only ever read one line. Fixed by making the
prompt unambiguous and adding a schema check that now warns loudly on a malformed classification
instead of silently guessing.

## V4 — automation arrives, with two providers evaluated honestly

Asked to compare Jarvis against azaris.ai (a paid, always-on, autonomously-acting cloud agent),
the team's answer was deliberate: chase the *experience* — phone access, persistent memory — and
explicitly reject the *mechanism* as permanently incompatible with Jarvis's principles, not just
deferred. `trafilatura` was evaluated for HTML extraction and rejected after checking its real
dependency tree (six packages including a C-extension); a 40-line stdlib extractor replaced it,
validated against genuinely captured raw HTML rather than synthetic samples. The Gemini API
provider was built only after searching Google's current documentation directly rather than
trusting training data, and its error handling was shaped by real developer reports of
inconsistent 404s even with valid keys — a known limitation (the $0 declaration is self-reported,
not independently verified) documented rather than hidden.

## The Telegram interface — and the first mutation-tested round

The first feature that carries a secret token and gates who can reach Jarvis remotely got an
unusual amount of scrutiny: every security-critical guard was deliberately broken and the test
suite re-run, to prove the tests would actually catch a regression. Four of nine didn't — tests
that passed without genuinely exercising their protection. All four rewritten, all nine
confirmed catching their mutation afterward. The scope itself was narrowed deliberately: read-only
only, because the existing confirmation mechanism (`input()`, blocking, in-process) has no way to
be safely sent to a phone and redeemed later without inventing new security machinery first.

## The most recent bug — caught live, fixed at the root

Testing the Telegram interface for real surfaced a crash: one git-inspection function checked
whether a project's directory existed before running, two others didn't. On Linux, a missing
directory raises one exception type; on Windows, a different one — and the second type wasn't
caught. The fix consolidated all four git methods through one shared pre-flight check, closing a
gap that could recur silently, and added the direct test coverage for `GitInspector` that hadn't
existed before despite the tool already being months old.

## An honest footnote on this document itself

While writing this file, the sandbox session it was being built in reset mid-task — everything in
that working copy was lost before it could be packaged. Rather than reconstruct from memory, the
actual repository was re-cloned from GitHub and verified (217 tests passing, matching Lone's own
confirmed run) before this file was added to it. A small, real example of the same discipline this
document describes: verify against the real state, don't trust a remembered one.

## What this adds up to

Sixteen real features and fixes, four frozen architectural decisions, and a documented habit of
catching mistakes — the team's own, and each other's — rather than hiding them. The constant
across every round: sandbox confidence is a starting point, not an ending point, and the real
machine gets the final word.

## What's still open

- A remote confirmation mechanism for Telegram — deliberately deferred, needs its own
  threat-modeling pass before any write action can be approved from a phone.
- An Ollama provider for local model execution — needs Lone to test real hardware feasibility
  before any code is written.
- `SUSPENDED`/`ABORTED` session handling — explicitly out of scope so far, not forgotten.
