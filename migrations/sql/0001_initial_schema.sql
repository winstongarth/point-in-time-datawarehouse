-- Five schemas with a strict one-way data flow: raw -> stg -> core, observed by
-- dq and ops.
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS stg;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS dq;
CREATE SCHEMA IF NOT EXISTS ops;

-- ops.pipeline_run: one row per invocation of any pipeline stage (ingest, parse,
-- load, dq). Every raw.payload row (M2) and dq.check_result row (M6) will carry a
-- run_id referencing this table, so it is created here in M1 even though most of
-- what it observes doesn't exist yet.
CREATE TABLE ops.pipeline_run (
    run_id     bigserial   PRIMARY KEY,
    pipeline   text        NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at   timestamptz,
    status     text        NOT NULL DEFAULT 'running',
    rows_in    bigint,
    rows_out   bigint,
    error      jsonb,
    CONSTRAINT pipeline_run_status_check
        CHECK (status IN ('running', 'success', 'failed'))
);

-- Runbook and freshness queries (M8) look up the most recent run per pipeline.
CREATE INDEX pipeline_run_pipeline_started_at_idx
    ON ops.pipeline_run (pipeline, started_at DESC);

COMMENT ON TABLE ops.pipeline_run IS
    'One row per pipeline invocation. raw.payload and dq.check_result rows trace '
    'back to a run_id here, giving full lineage from a core fact to the exact run '
    'that produced it.';
