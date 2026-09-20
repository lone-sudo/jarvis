import uuid
from pathlib import Path

from jarvis.state.database import get_connection, init_db

VALID_TASK_STATUSES = {"PENDING", "IN_PROGRESS", "AWAITING_USER", "BLOCKED", "DONE", "ABORTED"}


class StateTracker:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = init_db(db_path)

    def register_project(self, project_key: str, relative_root: str, display_name: str | None = None) -> None:
        posix_root = Path(relative_root).as_posix()
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO projects (project_key, relative_root, display_name)
                VALUES (?, ?, ?)
                ON CONFLICT(project_key) DO UPDATE SET
                    relative_root = excluded.relative_root,
                    display_name = excluded.display_name
                """,
                (project_key, posix_root, display_name or project_key),
            )

    def get_project_root(self, project_key: str) -> str | None:
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT relative_root FROM projects WHERE project_key = ?", (project_key,)
            ).fetchone()
            return row["relative_root"] if row else None

    def create_session(self, primary_goal: str) -> str:
        session_id = f"SES-{uuid.uuid4().hex[:8].upper()}"
        with get_connection(self.db_path) as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, primary_goal) VALUES (?, ?)",
                (session_id, primary_goal),
            )
        return session_id

    def create_task(
        self,
        session_id: str,
        title: str,
        project_key: str,
        relative_path: str = "",
        description: str = "",
    ) -> str:
        posix_path = Path(relative_path).as_posix() if relative_path else ""
        task_id = f"TSK-{uuid.uuid4().hex[:8].upper()}"
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO tasks (task_id, session_id, title, description, project_key, relative_path, status)
                VALUES (?, ?, ?, ?, ?, ?, 'PENDING')
                """,
                (task_id, session_id, title, description, project_key, posix_path),
            )
        return task_id

    def update_task_status(self, task_id: str, new_status: str) -> None:
        if new_status not in VALID_TASK_STATUSES:
            raise ValueError(f"Invalid status '{new_status}'. Must be one of {sorted(VALID_TASK_STATUSES)}.")
        with get_connection(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE task_id = ?",
                (new_status, task_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"No task found with id '{task_id}'.")

    def get_active_tasks(self) -> list[dict]:
        with get_connection(self.db_path) as conn:
            cur = conn.execute(
                "SELECT * FROM tasks WHERE status IN ('IN_PROGRESS', 'AWAITING_USER', 'PENDING') "
                "ORDER BY updated_at DESC"
            )
            return [dict(row) for row in cur.fetchall()]

    def log_execution(
        self,
        task_id: str,
        tool_name: str,
        action_type: str,
        status: str,
        input_payload: str = "",
        output_payload: str = "",
    ) -> None:
        execution_id = f"EXE-{uuid.uuid4().hex[:8].upper()}"
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO executions
                    (execution_id, task_id, tool_name, action_type, input_payload, output_payload, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (execution_id, task_id, tool_name, action_type, input_payload, output_payload, status),
            )
