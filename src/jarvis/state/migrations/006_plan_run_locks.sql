-- 006_plan_run_locks: one row per plan that a plan-run process currently owns (ADR-0005 amendment).
-- The PRIMARY KEY makes claiming a plan atomic across processes. owner_pid lets the next run
-- tell a live owner from a crashed one. owner_token makes release and takeover compare-and-delete.
-- Rows vanish with their plan. NOTE: the migration runner splits on semicolons, so keep them out of comments.

CREATE TABLE IF NOT EXISTS plan_run_locks (
    plan_id TEXT PRIMARY KEY,
    owner_pid INTEGER NOT NULL,
    owner_token TEXT NOT NULL,
    acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (plan_id) REFERENCES execution_plans(plan_id) ON DELETE CASCADE
);
