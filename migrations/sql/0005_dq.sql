-- dq.check_result: every check writes a row every run, including passes
-- (a check that only records failures cannot support a
-- coverage metric).
CREATE TABLE dq.check_result (
    check_id     bigserial   PRIMARY KEY,
    check_name   text        NOT NULL,
    dataset      text        NOT NULL,
    run_id       bigint      NOT NULL REFERENCES ops.pipeline_run (run_id),
    severity     text        NOT NULL,
    status       text        NOT NULL,
    observed     jsonb,
    expected     jsonb,
    evaluated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT check_result_severity_check CHECK (severity IN ('INFO', 'WARN', 'BREAK')),
    CONSTRAINT check_result_status_check CHECK (status IN ('pass', 'fail'))
);

CREATE INDEX check_result_name_evaluated_idx
    ON dq.check_result (check_name, evaluated_at DESC);

-- dq.exception: the triage lifecycle for a *specific, recurring* failure
-- (identified by check_name + dimension_key, e.g. a ticker or ticker+period),
-- separate from check_result's per-run log. A failure opens one; the same
-- failure recurring on a later run leaves it open; the check passing again
-- auto-closes it. "triage" is a manual, human-acknowledged middle state.
CREATE TABLE dq.exception (
    exception_id    bigserial   PRIMARY KEY,
    check_id        bigint      NOT NULL REFERENCES dq.check_result (check_id),
    entity_id       int         REFERENCES core.entity (entity_id),
    dimension_key   text        NOT NULL,
    severity        text        NOT NULL,
    status          text        NOT NULL DEFAULT 'open',
    opened_at       timestamptz NOT NULL DEFAULT now(),
    closed_at       timestamptz,
    resolution_note text,

    CONSTRAINT exception_severity_check CHECK (severity IN ('INFO', 'WARN', 'BREAK')),
    CONSTRAINT exception_status_check CHECK (status IN ('open', 'triage', 'closed'))
);

-- The lookup this whole lifecycle depends on: "is there already an open (or
-- in-triage) exception for this check+dimension?" - joined through check_id
-- to check_result for check_name, so this index supports that join's other
-- side.
CREATE INDEX exception_check_id_idx ON dq.exception (check_id);
CREATE INDEX exception_dimension_status_idx ON dq.exception (dimension_key, status);
