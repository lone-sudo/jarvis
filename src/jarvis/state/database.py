import sqlite3
from pathlib import Path

from jarvis.policy.rules import SecurityPolicy
from jarvis.state.migration_runner import apply_pending_migrations


def default_db_path() -> Path:
    """
    Jarvis's own state lives beside the workspace, not wherever the CLI
    happens to be launched from. Gemini's original StateTracker used a
    bare relative "jarvis_state.db", which means running `jarvis resume`
    from two different terminal locations creates two different,
    silently disconnected databases — a real bug, not a style nit.
    """
    root = SecurityPolicy.get_workspace_root().parent
    return root / ".jarvis" / "jarvis_state.db"


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """
    Every connection must re-enable foreign key enforcement — SQLite
    does not persist PRAGMA foreign_keys across connections. Gemini's
    original code only ran it once, inside schema init, so every
    subsequent insert/delete after that silently ran with FK
    enforcement OFF. That means a task could be inserted against a
    session_id that doesn't exist, and cascade deletes wouldn't fire —
    exactly the kind of state-integrity bug this milestone exists to
    prevent.
    """
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path | None = None) -> Path:
    """
    Superseded the old executescript(schema.sql) approach — see
    build-log 0007. Now applies numbered migrations from
    state/migrations/ in order, tracked via a schema_version table,
    each in its own transaction. Safe to call on every StateTracker
    init: pending migrations apply, already-applied ones are skipped.
    """
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    apply_pending_migrations(path)
    return path
