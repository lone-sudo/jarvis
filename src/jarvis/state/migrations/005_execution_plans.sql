-- 005_execution_plans: durable storage for M002 execution plans (ADR-0005).
-- plan_json is the canonical serialized plan. status, current_step_index and
-- max_steps are denormalized copies kept in sync by plan_store on every save,
-- present only so plans can be listed and filtered without parsing JSON.
-- NOTE: the migration runner splits on semicolons, so keep them out of comments.

CREATE TABLE IF NOT EXISTS execution_plans (
    plan_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    status TEXT CHECK(status IN ('PENDING', 'RUNNING', 'COMPLETED', 'BLOCKED')) NOT NULL DEFAULT 'PENDING',
    current_step_index INTEGER NOT NULL DEFAULT 0,
    max_steps INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_execution_plans_task ON execution_plans(task_id);
CREATE INDEX IF NOT EXISTS idx_execution_plans_status ON execution_plans(status);
