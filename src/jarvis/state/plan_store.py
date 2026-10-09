"""
SQLite persistence for execution plans (migration 005, ADR-0005).

plan_json is canonical. The status / current_step_index / max_steps
columns are written from the same dataclass in the same statement, so
they cannot drift from it, and load() reads only plan_json.
"""

import json
from pathlib import Path

from jarvis.core.plan import ExecutionPlan, PlanFormatError
from jarvis.state.database import get_connection, init_db


class PlanNotFound(LookupError):
    pass


def get_task_row(db_path: str | Path, task_id: str) -> dict | None:
    """Plain task lookup (StateTracker has no single-task getter)."""
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


class PlanStore:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = init_db(db_path)

    def create(self, plan: ExecutionPlan) -> ExecutionPlan:
        """
        Validates, then inserts. Raises PlanFormatError for a malformed
        plan and ValueError if the task does not exist (checked
        explicitly so the message names the task, rather than surfacing
        a bare foreign-key IntegrityError).
        """
        plan.validate_shape()
        if get_task_row(self.db_path, plan.task_id) is None:
            raise ValueError(f"No task found with id '{plan.task_id}'.")
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO execution_plans
                    (plan_id, task_id, status, current_step_index, max_steps, plan_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (plan.plan_id, plan.task_id, plan.status.value, plan.current_step_index,
                 plan.max_steps, plan.to_json()),
            )
        return plan

    def save(self, plan: ExecutionPlan) -> None:
        with get_connection(self.db_path) as conn:
            cur = conn.execute(
                """
                UPDATE execution_plans
                   SET status = ?, current_step_index = ?, max_steps = ?,
                       plan_json = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE plan_id = ?
                """,
                (plan.status.value, plan.current_step_index, plan.max_steps,
                 plan.to_json(), plan.plan_id),
            )
            if cur.rowcount == 0:
                raise PlanNotFound(f"No plan found with id '{plan.plan_id}'.")

    def load(self, plan_id: str) -> ExecutionPlan:
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT plan_json FROM execution_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            raise PlanNotFound(f"No plan found with id '{plan_id}'.")
        try:
            return ExecutionPlan.from_json(row["plan_json"])
        except PlanFormatError as e:
            raise PlanFormatError(f"Stored plan '{plan_id}' is corrupt: {e}") from e

    def list_for_task(self, task_id: str) -> list[ExecutionPlan]:
        with get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT plan_json FROM execution_plans WHERE task_id = ? ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
        return [ExecutionPlan.from_json(r["plan_json"]) for r in rows]

    def get_failure_diagnostics(self, plan_id: str, task_id: str) -> dict | None:
        """
        Latest preserved failure record for a plan, from the executions
        audit table (where _preserve_failure writes it). Returns the
        parsed diagnostics dict, or None. Filtering by plan happens in
        Python on parsed JSON, not with a LIKE pattern on payload text.
        """
        with get_connection(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT input_payload, output_payload FROM executions
                 WHERE task_id = ? AND tool_name = 'engine:failure'
                 ORDER BY executed_at DESC, rowid DESC
                """,
                (task_id,),
            ).fetchall()
        for row in rows:
            try:
                meta = json.loads(row["input_payload"] or "{}")
                if meta.get("plan_id") == plan_id:
                    return json.loads(row["output_payload"])
            except json.JSONDecodeError:
                continue
        return None
