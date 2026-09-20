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

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_key);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
