"""
Marking a task DONE (M002.1, ADR-0005 (e)).

DONE is terminal: the state machine has no way back, and the engine refuses to run any plan against
a DONE task. So it is a confirmed mutation like write-file and git-commit (AGENTS.md invariant 3):
the person sees what they are closing and answers an interactive [y/N].

Every path that can set DONE from the CLI goes through complete_task (`task-done`, and `ask-ai --done`
after the response is shown). Nothing here is inferred from a response or from a plan finishing:
a COMPLETED plan never completes its task, because the task may have other work outstanding.

Result strings follow the tools' convention: "ERROR: ..." for a refusal, with "(declined by user)"
for a "no", so run_logged records it as REJECTED_BY_POLICY.
"""

from typing import Callable

from jarvis.state.plan_store import PlanStore, get_task_row
from jarvis.state.tracker import ALLOWED_TRANSITIONS, StateTracker


def _confirm(prompt: str) -> bool:
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def complete_task(
    tracker: StateTracker,
    task_id: str,
    *,
    auto_confirm: "bool | None" = None,   # tests only: None means a real interactive prompt
    out: Callable[[str], None] = print,
) -> str:
    row = get_task_row(tracker.db_path, task_id)
    if row is None:
        return f"ERROR: No task found with id '{task_id}'. Nothing changed."

    status = row["status"]
    if status == "DONE":
        return f"Task {task_id} is already DONE. Nothing changed."
    if "DONE" not in ALLOWED_TRANSITIONS.get(status, set()):
        allowed = sorted(ALLOWED_TRANSITIONS.get(status, set())) or "(terminal)"
        return (
            f"ERROR: Task {task_id} is {status} and cannot be marked DONE from there "
            f"(allowed from {status}: {allowed}). Nothing changed."
        )

    plans = PlanStore(tracker.db_path).list_for_task(task_id)
    out(f"Task {task_id}: {row['title']}")
    out(f"  project: {row['project_key']}   status: {status}")
    if plans:
        for plan in plans:
            out(f"  plan {plan.plan_id}: {plan.status.value} ({plan.current_step_index}/{len(plan.steps)} steps)")
    else:
        out("  plans: none")
    unfinished = [p.plan_id for p in plans if p.status.value != "COMPLETED"]
    if unfinished:
        out(f"  WARNING: {len(unfinished)} plan(s) not COMPLETED: {', '.join(unfinished)}. "
            f"Marking the task DONE means none of them can be run again.")
    out("  DONE is permanent. It cannot be reopened.")

    confirmed = auto_confirm if auto_confirm is not None else _confirm(f"Mark task {task_id} DONE?")
    if not confirmed:
        return f"ERROR: Task {task_id} left as {status} (declined by user)."

    try:
        tracker.update_task_status(task_id, "DONE")
    except ValueError as e:
        # The task changed state while the prompt was open.
        return f"ERROR: {e}"
    return f"Task {task_id} is now DONE."
