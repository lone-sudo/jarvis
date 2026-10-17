-- Skills v1 (ADR-0006): approval state for text-only skills, plus an append-only event log.
-- A skill that is not in this table has never been approved. Content lives in Markdown on disk.
CREATE TABLE IF NOT EXISTS skills (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT CHECK(status IN ('approved', 'disabled')) NOT NULL,
    approved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    snapshot_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    event TEXT NOT NULL,
    content_hash TEXT,
    detail TEXT,
    at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
