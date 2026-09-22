-- 002_inbox: V2 content inbox. Same IF NOT EXISTS safety rationale as 001.

CREATE TABLE IF NOT EXISTS inbox_items (
    item_id TEXT PRIMARY KEY,
    status TEXT CHECK(status IN ('UNPROCESSED', 'PROCESSED', 'ARCHIVED')) DEFAULT 'UNPROCESSED',
    source_url TEXT,
    title TEXT,
    note TEXT,
    relative_markdown_path TEXT,
    project_key TEXT,                   -- nullable; set = CONFIRMED link (only via `jarvis inbox-link`). Classification alone never sets this.
    suggested_project_key TEXT,         -- classifier's guess, advisory only, never auto-promoted to project_key
    tags TEXT,
    summary TEXT,
    actionable INTEGER CHECK(actionable IN (0, 1)),
    confidence REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP,
    FOREIGN KEY (project_key) REFERENCES projects(project_key) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox_items(status);
CREATE INDEX IF NOT EXISTS idx_inbox_project ON inbox_items(project_key);
