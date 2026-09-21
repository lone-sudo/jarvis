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
    fn()'s result unchanged. fn should return a string; if it starts
    with "ERROR: Access denied by policy", the execution is logged as
    REJECTED_BY_POLICY rather than SUCCESS.
    """
    try:
        result = fn()
    except Exception as e:
        tracker.log_execution(
            task_id=task_id, tool_name=tool_name, action_type=action_type,
            status="FAILURE", input_payload=input_summary, output_payload=str(e),
        )
        raise

    if isinstance(result, str) and "denied by policy" in result.lower():
        status = "REJECTED_BY_POLICY"
    elif isinstance(result, str) and result.startswith("ERROR:"):
        status = "FAILURE"
    else:
        status = "SUCCESS"

    tracker.log_execution(
        task_id=task_id, tool_name=tool_name, action_type=action_type,
        status=status, input_payload=input_summary,
        output_payload=(result if isinstance(result, str) else str(result))[:2000],
    )
    return result
