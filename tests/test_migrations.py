import shutil
import sqlite3
from pathlib import Path

import pytest

import jarvis.state.migration_runner as runner


@pytest.fixture
def real_migrations_content():
    return list(runner.MIGRATIONS_DIR.glob("*.sql"))


@pytest.fixture
def legacy_era_migrations_content():
    """
    ONLY the migrations that existed before the migration system itself
    was introduced (001+002 -- see build-log 0007). A genuinely legacy
    pre-migration-system database can only ever have this shape; any
    database containing content from migration 003 or later would
    necessarily have been created THROUGH the runner, meaning it
    already has a schema_version row. Using "all current migrations"
    here (as an earlier version of this fixture did) was itself a bug:
    it silently broke the moment a later migration (004, ALTER TABLE,
    not idempotent) was added, because re-applying an ALTER TABLE the
    legacy-simulation had already baked in via raw executescript
    correctly failed with "duplicate column" -- a real signal that the
    test no longer represented a real scenario, not a bug in 004 itself.
    """
    return [f for f in runner.MIGRATIONS_DIR.glob("*.sql") if f.stem.split("_", 1)[0] in ("001", "002")]


def test_fresh_database_applies_all_migrations(tmp_path):
    db_path = tmp_path / "fresh.db"
    final_version = runner.apply_pending_migrations(db_path)

    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "schema_version" in tables
    assert "sessions" in tables
    assert "inbox_items" in tables
    assert final_version == max(num for num, _ in runner._discover_migrations())


def test_applying_twice_is_idempotent(tmp_path):
    db_path = tmp_path / "twice.db"
    v1 = runner.apply_pending_migrations(db_path)
    v2 = runner.apply_pending_migrations(db_path)  # nothing pending second time
    assert v1 == v2


def test_legacy_database_migrates_without_data_loss(tmp_path, legacy_era_migrations_content):
    """
    The exact scenario that matters most: a real V2 database created
    by the OLD executescript(schema.sql) path, with real user data,
    and no schema_version table. Migrating it must not lose or alter
    that data.
    """
    db_path = tmp_path / "legacy.db"
    combined_sql = "\n".join(f.read_text() for f in sorted(legacy_era_migrations_content))
    conn = sqlite3.connect(db_path)
    conn.executescript(combined_sql)
    conn.execute("INSERT INTO sessions (session_id, primary_goal) VALUES ('SES-REAL', 'real data')")
    conn.execute("INSERT INTO projects (project_key, relative_root) VALUES ('proj-a', 'proj-a')")
    conn.commit()
    conn.close()

    final_version = runner.apply_pending_migrations(db_path)
    assert final_version == max(num for num, _ in runner._discover_migrations())

    conn = sqlite3.connect(db_path)
    sessions = conn.execute("SELECT session_id, primary_goal FROM sessions").fetchall()
    projects = conn.execute("SELECT project_key FROM projects").fetchall()
    assert sessions == [("SES-REAL", "real data")]
    assert projects == [("proj-a",)]


def test_failed_migration_rolls_back_and_does_not_advance_version(tmp_path, monkeypatch):
    test_dir = tmp_path / "test_migrations"
    test_dir.mkdir()
    for f in runner.MIGRATIONS_DIR.glob("*.sql"):
        shutil.copy(f, test_dir / f.name)
    real_max = max(num for num, _ in runner._discover_migrations())
    (test_dir / "999_broken.sql").write_text(
        "CREATE TABLE should_not_persist (x INTEGER);\n"
        "CREATE TABLE this is not valid sql;\n"
    )
    monkeypatch.setattr(runner, "MIGRATIONS_DIR", test_dir)

    db_path = tmp_path / "failure.db"
    with pytest.raises(runner.MigrationError):
        runner.apply_pending_migrations(db_path)

    conn = sqlite3.connect(db_path)
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == real_max  # every real migration applied; 999 failed and did not advance past it
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "should_not_persist" not in tables  # no partial application


def test_retry_after_fixed_migration_succeeds(tmp_path, monkeypatch):
    test_dir = tmp_path / "test_migrations"
    test_dir.mkdir()
    for f in runner.MIGRATIONS_DIR.glob("*.sql"):
        shutil.copy(f, test_dir / f.name)
    real_max = max(num for num, _ in runner._discover_migrations())
    broken_path = test_dir / "999_broken.sql"
    broken_path.write_text("CREATE TABLE t (x INTEGER);\nCREATE TABLE this is not valid sql;\n")
    monkeypatch.setattr(runner, "MIGRATIONS_DIR", test_dir)

    db_path = tmp_path / "retry.db"
    with pytest.raises(runner.MigrationError):
        runner.apply_pending_migrations(db_path)

    # "Fix" the migration and retry -- same db, same process, as if
    # Jarvis were restarted after the user corrected the problem.
    broken_path.write_text("CREATE TABLE t (x INTEGER);\nINSERT INTO t VALUES (1);\n")
    final_version = runner.apply_pending_migrations(db_path)
    assert final_version == 999  # the injected migration's own number, applied last

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT * FROM t").fetchall() == [(1,)]


def test_migrations_apply_in_numeric_order_not_lexical(tmp_path, monkeypatch):
    test_dir = tmp_path / "order_test"
    test_dir.mkdir()
    (test_dir / "1_first.sql").write_text("CREATE TABLE step1 (x INTEGER);")
    (test_dir / "10_tenth.sql").write_text("CREATE TABLE step10 (x INTEGER);")
    (test_dir / "2_second.sql").write_text("CREATE TABLE step2 (x INTEGER);")
    monkeypatch.setattr(runner, "MIGRATIONS_DIR", test_dir)

    db_path = tmp_path / "order.db"
    final_version = runner.apply_pending_migrations(db_path)
    assert final_version == 10  # not lexically "2" < "10" mis-ordering
