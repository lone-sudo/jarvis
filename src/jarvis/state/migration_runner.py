"""
Narrow migration system. No ORM, no Alembic — per the frozen design,
this stays small on purpose.

IMPORTANT (found by testing, not assumed): sqlite3.Connection.executescript()
does not respect an enclosing manual transaction the way you'd expect —
verified empirically that a failing multi-statement script can leave an
earlier statement's effects committed even when wrapped in an explicit
BEGIN/ROLLBACK. So migrations are applied statement-by-statement via
individual conn.execute() calls inside a manually managed transaction,
never via executescript(). See build-log 0007 for the reproduction.

Migration files are trusted application resources shipped with Jarvis
(read from this package's own migrations/ directory) — never arbitrary
user-controlled SQL, and never fetched from anywhere external.
"""

import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class MigrationError(Exception):
    """Raised when a migration fails and is rolled back."""


def _split_statements(sql: str) -> list[str]:
    """
    Splits a trusted DDL migration file into individual statements.
    Strips '--' line comments first (our migrations are plain CREATE
    TABLE/INDEX statements with no string literals containing
    semicolons, so a straightforward split is safe here — this is not
    a general-purpose SQL parser and isn't meant to be).
    """
    no_comments = re.sub(r"--.*", "", sql)
    statements = [s.strip() for s in no_comments.split(";")]
    return [s for s in statements if s]


def _discover_migrations() -> list[tuple[int, Path]]:
    migrations = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        num_str = f.stem.split("_", 1)[0]
        try:
            num = int(num_str)
        except ValueError:
            continue
        migrations.append((num, f))
    return sorted(migrations, key=lambda pair: pair[0])


def _get_current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (0)")
        return 0
    return row[0]


def apply_pending_migrations(db_path: Path) -> int:
    """
    Applies every migration numbered higher than the database's current
    schema_version, strictly in order. Each migration runs inside its
    own transaction: on success, schema_version advances to that
    migration's number and commits; on failure, the transaction rolls
    back, schema_version is left unchanged, and a MigrationError is
    raised immediately -- later migrations are never attempted once one
    fails. A subsequent call (e.g. the next time Jarvis starts) will
    retry from the same unchanged version.

    Returns the final schema_version after all pending migrations
    succeed.
    """
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None  # manual transaction control -- see module docstring
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        current = _get_current_version(conn)

        for number, path in _discover_migrations():
            if number <= current:
                continue

            statements = _split_statements(path.read_text(encoding="utf-8"))
            conn.execute("BEGIN")
            try:
                for stmt in statements:
                    conn.execute(stmt)
                conn.execute("UPDATE schema_version SET version = ?", (number,))
                conn.execute("COMMIT")
                current = number
            except Exception as e:
                conn.execute("ROLLBACK")
                raise MigrationError(
                    f"Migration {path.name} failed and was rolled back cleanly. "
                    f"schema_version remains at {current}. Original error: {e}"
                ) from e

        return current
    finally:
        conn.close()
