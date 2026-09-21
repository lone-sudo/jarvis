import json
import uuid

from jarvis.state.database import get_connection

VALID_INBOX_STATUSES = {"UNPROCESSED", "PROCESSED", "ARCHIVED"}


class InboxStore:
    """
    Kept separate from StateTracker (which owns sessions/tasks) since
    inbox items are a distinct lifecycle — content the user saved, not
    work being tracked. A saved item never becomes a task automatically;
    see interface/cli.py::cmd_inbox_process and cmd_inbox_link for the
    only places a human can explicitly bridge the two.
    """

    def __init__(self, db_path):
        self.db_path = db_path

    def create_item(self, title: str, source_url: str, note: str, relative_markdown_path: str) -> str:
        item_id = f"INB-{uuid.uuid4().hex[:8].upper()}"
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO inbox_items (item_id, status, source_url, title, note, relative_markdown_path)
                VALUES (?, 'UNPROCESSED', ?, ?, ?, ?)
                """,
                (item_id, source_url, title, note, relative_markdown_path),
            )
        return item_id

    def get_item(self, item_id: str) -> dict | None:
        with get_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM inbox_items WHERE item_id = ?", (item_id,)).fetchone()
            return dict(row) if row else None

    def list_items(self, status: str | None = None) -> list[dict]:
        with get_connection(self.db_path) as conn:
            if status:
                if status not in VALID_INBOX_STATUSES:
                    raise ValueError(f"Invalid status '{status}'. Must be one of {sorted(VALID_INBOX_STATUSES)}.")
                cur = conn.execute(
                    "SELECT * FROM inbox_items WHERE status = ? ORDER BY created_at", (status,)
                )
            else:
                cur = conn.execute("SELECT * FROM inbox_items ORDER BY created_at")
            return [dict(row) for row in cur.fetchall()]

    def mark_processed(
        self, item_id: str, *, summary: str, tags: list[str], actionable: bool,
        suggested_project_key: str | None, confidence: float,
    ) -> None:
        with get_connection(self.db_path) as conn:
            cur = conn.execute(
                """
                UPDATE inbox_items
                SET status = 'PROCESSED', summary = ?, tags = ?, actionable = ?,
                    suggested_project_key = ?, confidence = ?, processed_at = CURRENT_TIMESTAMP
                WHERE item_id = ?
                """,
                (summary, json.dumps(tags), int(actionable), suggested_project_key, confidence, item_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"No inbox item found with id '{item_id}'.")

    def link_to_project(self, item_id: str, project_key: str) -> None:
        """
        The ONLY way project_key gets set. Deliberately separate from
        classification (mark_processed only touches suggested_project_key)
        so an AI's guess never silently becomes a real link — a human
        calls this explicitly, typically after reviewing the suggestion
        via `jarvis inbox`.
        """
        with get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE inbox_items SET project_key = ? WHERE item_id = ?", (project_key, item_id)
            )

    def archive(self, item_id: str) -> None:
        with get_connection(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE inbox_items SET status = 'ARCHIVED' WHERE item_id = ?", (item_id,)
            )
            if cur.rowcount == 0:
                raise ValueError(f"No inbox item found with id '{item_id}'.")
