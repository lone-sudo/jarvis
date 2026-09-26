"""
Session/task consolidation: the "nightly wrap-up" concept from the
original brief, built manual-trigger-only per converged review
(ChatGPT + Claude majority over Gemini's schema-change proposal,
per Lone's governance-rule decision).

Ownership is derived from a session's tasks at consolidation time,
NOT stored on sessions itself -- every task already has a mandatory
project_key (NOT NULL in schema), so a session's ownership is simply
"do all its tasks agree on one project_key." No new join complexity,
no schema change to sessions beyond the idempotency flag.
"""

from dataclasses import dataclass, field


@dataclass
class SessionConsolidationCandidate:
    session: dict
    project_key: str | None  # None means ineligible
    tasks: list = field(default_factory=list)
    ineligible_reason: str | None = None  # set when project_key is None


def determine_ownership(tasks: list[dict]) -> tuple[str | None, str | None]:
    """
    Returns (project_key, ineligible_reason). Exactly one of the two is
    None. A session is eligible only if it has at least one task and
    every task agrees on the same project_key.
    """
    if not tasks:
        return None, "session has no tasks"

    distinct_keys = {t["project_key"] for t in tasks}
    if len(distinct_keys) > 1:
        return None, f"tasks span multiple projects ({', '.join(sorted(distinct_keys))})"

    return distinct_keys.pop(), None


def build_candidate(session: dict, tasks: list[dict]) -> SessionConsolidationCandidate:
    project_key, reason = determine_ownership(tasks)
    return SessionConsolidationCandidate(
        session=session, project_key=project_key, tasks=tasks, ineligible_reason=reason
    )


def build_consolidated_note(candidate: SessionConsolidationCandidate) -> str:
    """
    PROJECT STATE / DECISIONS / KNOWN ISSUES style summary, per the
    original brief's section 17 sketch. Only called for eligible
    candidates (project_key is not None).
    """
    session = candidate.session
    lines = [f"## Session Wrap-up: {session['primary_goal']}"]
    lines.append(f"Session: {session['session_id']} (completed)")
    lines.append("")
    lines.append("### Tasks")
    for task in candidate.tasks:
        mark = "x" if task["status"] == "DONE" else "!" if task["status"] == "ABORTED" else "?"
        lines.append(f"- [{mark}] {task['title']} ({task['status']})")
    done_count = sum(1 for t in candidate.tasks if t["status"] == "DONE")
    aborted_count = sum(1 for t in candidate.tasks if t["status"] == "ABORTED")
    lines.append("")
    lines.append(f"### Outcome\n{done_count} completed, {aborted_count} aborted, {len(candidate.tasks)} total.")
    return "\n".join(lines)
