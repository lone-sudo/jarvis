# 0018 — Evaluation of ten external tools (read-only, plus two isolated trials)

**Date:** 2026-10-11
**Contributor:** Claude
**Branch:** `feature/claude-tool-evaluation` (documentation only; no source, policy or ADR file touched)
**Status:** Recommendation delivered to Lone. Nothing was installed globally and nothing was adopted. Three proposals await his approval (below).

## Question
Which of Find Skills, Prompt Master, Agent Reach, Claude-Mem, Headroom, Claude Code Setup, Task Observer, Graphify, Agent Skills and Ponytail, if any, should join the development workflow or Jarvis, given our invariants (Jarvis owns state and memory, `[y/N]` for mutations, $0 spending, no LiteLLM, V3 fetch safeguards).

## Method
All ten repositories were cloned as text into a scratch folder and read (README, install docs, hooks, dependency files, licence). Nothing was executed from them except two trials: Graphify 0.9.85 in a separate virtual environment, and Ponytail's rule text given to one subagent (its hooks were never installed). The GitHub API was closed for these repositories, so activity came from git history and star counts were not used.

## Classification
| Tool | Class | Deciding evidence |
|---|---|---|
| Find Skills | Defer | CLI plus one SKILL.md; telemetry to skills.sh on by default; no unmet skill need |
| Prompt Master | Defer | Text only; no measurable gain over our task-spec handoff |
| Agent Reach | Reject | Core value is cookies, logged-in sessions, proxies and a Chrome extension; install guide is a URL the agent is told to follow |
| Claude-Mem | Reject | Competing memory store; hooks on every tool call; hosted observer after sign-in |
| Headroom | Reject | `litellm` is a core dependency (ADR bans it); default-on beacon; `headroom learn` writes CLAUDE.md/AGENTS.md; lossy |
| Claude Code Setup | Reject the auto-setup prompt | Tells the agent not to ask for confirmation, then writes permissions and hooks; two commits, last Feb 2026 |
| Task Observer | Defer | Second log beside the build journal; CC BY 4.0 attribution; built for large skill libraries |
| Graphify | Defer | Works locally (2.8 s, 1,273 nodes) but matched grep on a 5,000-line repo |
| Agent Skills | Test selected skills | Text content, MIT, hooks not wired by default; overlaps AGENTS.md |
| Ponytail | Test in isolation | See below |

## Ponytail isolated test
Two scratch clones at `f2190cb`; same task (add `jarvis plan-export PLAN_ID PATH`); arm B also got Ponytail's skill text. Eight hidden checks written first and validated: a correct reference passes 8/8, a version that skips `write_file` fails 4.

| | Baseline | Ponytail |
|---|---|---|
| Lines added, src / tests | 36 / 31 | 21 / 17 |
| Suite | 418 passed | 416 passed |
| Hidden checks | 6/8 | 5/8 |
| Subagent tokens | 70,745 | 71,234 |

Findings: both arms wrote the file directly and skipped our `[y/N]` write tool (both said so in their reports). The baseline added an overwrite guard and created parent folders; the Ponytail arm silently overwrites and fails when the folder is missing. One task, one run per arm: an anecdote. Vendor numbers (53% less code, 26% lower cost) are author-run with CLAUDE.md off and were not reproduced.

## Failures and lessons
- **My reference implementation used a method that does not exist** (`plan.to_dict`; the real one is `to_json`), so my first validation run made the hidden checks look broken. Lesson: validate the checks against a known-good implementation before judging anyone with them, and read a failing validation as a possible bug in the reference first.
- **Both arms bypassed the confirmed-write tool.** Our rule is in AGENTS.md as an invariant, but nothing tells an author that a new CLI command must call `FileSystemInspector.write_file`. Proposal 1 below.
- A GitHub star count from a small-model page summary was not used: unverifiable.
- The brief and the repository disagree on roles (sole coding agent vs the AGENTS.md team) and on V3 status (repository already has `tools/network.py`, `network-allow`, `save --fetch`). Followed AGENTS.md; flagged to Lone.

## Proposals needing Lone's approval
1. Add to AGENTS.md: a CLI command that writes a file must go through `FileSystemInspector.write_file`.
2. Run a wider Ponytail test (5 tasks, 3 with a safety boundary, 2 runs per arm, our AGENTS.md loaded in both arms).
3. Merge this entry.

## Not verified
Node/npm on the ZBook (session not linked to it); star and install counts; every vendor benchmark; any tool other than Graphify and the Ponytail rule text, none of which was run on Windows.

## Cleanup
Scratch clones, the Graphify virtual environment and the A/B arms were created under the session scratchpad only and removed afterwards. Nothing under `~/.claude`, `~/.config` or the Jarvis repository changed except this file.
