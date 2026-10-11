"""
Shared execution-logging wrapper. Every tool call — read-only or
write — should pass through here so the `executions` table is a
complete, trustworthy audit log, not something each tool remembers
to write to individually.
"""

from jarvis.state.tracker import StateTracker


def run_logged(tracker: StateTracker, task_id: str, tool_name: str, action_type: str, fn, *, input_summary: str = ""):
    """
    Calls fn() (no args), logs the outcome against task_id, and returns
    fn()'s result unchanged. String results beginning with "ERROR:" are
    classified as REJECTED_BY_POLICY when they contain a policy denial or
    user decline; other error strings are logged as FAILURE.
    """
    try:
        result = fn()
    except Exception as e:
        tracker.log_execution(
            task_id=task_id, tool_name=tool_name, action_type=action_type,
            status="FAILURE", input_payload=input_summary, output_payload=str(e),
        )
        raise

    if not isinstance(result, str) or not result.startswith("ERROR:"):
        status = "SUCCESS"
    elif "denied by policy" in result.lower() or "(declined by user)" in result:
        status = "REJECTED_BY_POLICY"
    else:
        status = "FAILURE"

    tracker.log_execution(
        task_id=task_id, tool_name=tool_name, action_type=action_type,
        status=status, input_payload=input_summary,
        output_payload=(result if isinstance(result, str) else str(result))[:2000],
    )
    return result
