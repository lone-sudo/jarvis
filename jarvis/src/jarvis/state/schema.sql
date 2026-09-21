PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    primary_goal TEXT NOT NULL,
    status TEXT CHECK(status IN ('ACTIVE', 'SUSPENDED', 'COMPLETED')) DEFAULT 'ACTIVE'
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    project_key TEXT NOT NULL,
    relative_path TEXT,                 -- POSIX path, relative to workspace root
    status TEXT CHECK(status IN ('PENDING', 'IN_PROGRESS', 'AWAITING_USER', 'BLOCKED', 'DONE', 'ABORTED')) DEFAULT 'PENDING',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    action_type TEXT CHECK(action_type IN ('READ_ONLY', 'SAFE_WRITE', 'EXTERNAL')) NOT NULL,
    input_payload TEXT,
    output_payload TEXT,
    status TEXT CHECK(status IN ('SUCCESS', 'FAILURE', 'REJECTED_BY_POLICY')) NOT NULL,
    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    git_branch TEXT,
    git_commit_hash TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

-- Registry of known projects, mapping a short project_key to its actual
-- location under the workspace root. Without this, task.project_key
-- (e.g. "structural-rcc-suite") has no reliable link to a real directory,
-- which breaks "where did I leave off" the first time a project folder
-- name doesn't exactly match its key.
CREATE TABLE IF NOT EXISTS projects (
    project_key TEXT PRIMARY KEY,
    relative_root TEXT NOT NULL,        -- POSIX path, relative to workspace root
    display_name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- V2: Content Inbox. Manual-save only (no scraping, no auto-fetch — the
-- user types or pastes what they want captured). Lifecycle collapsed to
-- three states per ChatGPT/Gemini's converged review: LINKED/UNLINKED is
-- not a separate state, just whether project_key is set.
CREATE TABLE IF NOT EXISTS inbox_items (
    item_id TEXT PRIMARY KEY,
    status TEXT CHECK(status IN ('UNPROCESSED', 'PROCESSED', 'ARCHIVED')) DEFAULT 'UNPROCESSED',
    source_url TEXT,                    -- nullable: a raw note has no URL
    title TEXT,
    note TEXT,                          -- the user's own reason/context for saving it
    relative_markdown_path TEXT,        -- POSIX path under workspace/inbox/, holds the full content
    project_key TEXT,                   -- nullable; set = CONFIRMED link (only via `jarvis inbox link`). Classification alone never sets this.
    suggested_project_key TEXT,         -- classifier's guess, advisory only, never auto-promoted to project_key
    tags TEXT,                          -- JSON array, e.g. '["postgresql", "data-engineering"]'
    summary TEXT,                       -- short AI-generated summary, cached here for fast listing
    actionable INTEGER CHECK(actionable IN (0, 1)),  -- classifier's suggestion only — never auto-creates a task
    confidence REAL,                    -- 0.0-1.0, classifier's self-reported confidence
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP,
    FOREIGN KEY (project_key) REFERENCES projects(project_key) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox_items(status);
CREATE INDEX IF NOT EXISTS idx_inbox_project ON inbox_items(project_key);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_key);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
