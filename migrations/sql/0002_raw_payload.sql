-- raw.payload: the immutable landing zone. Every core fact must be
-- traceable back to a byte-identical vendor response via payload_id, so
-- every ingestion adapter writes here before anything is parsed.
CREATE TABLE raw.payload (
    payload_id     bigserial   PRIMARY KEY,
    source         text        NOT NULL,
    endpoint       text        NOT NULL,
    request_params jsonb       NOT NULL,
    fetched_at     timestamptz NOT NULL,
    http_status    int         NOT NULL,
    content_sha256 char(64)    NOT NULL,
    body           bytea       NOT NULL,
    run_id         bigint      NOT NULL REFERENCES ops.pipeline_run (run_id)
);

-- Every fetch is recorded even when content is unchanged (a repeated hash is
-- itself information), so this index is what lets the
-- parsing stage cheaply recognize "we've already seen this exact body"
-- without re-reading the bytea payload.
CREATE INDEX raw_payload_source_hash_idx ON raw.payload (source, content_sha256);

-- raw.payload is append-only: it is the audit trail underpinning every fact
-- in core, so no row may ever be changed or removed after it lands.
--
-- Below, a doubled percent character is one single RAISE format placeholder
-- for TG_OP, doubled only because this whole file is executed through
-- psycopg (via Alembic's exec_driver_sql). psycopg client-side-scans the
-- entire submitted text (comments included) for percent-sign placeholders
-- and requires a literal percent character to be doubled; it un-escapes the
-- doubled pair back to one percent character before the query reaches
-- Postgres, so RAISE still sees exactly one placeholder there and
-- substitutes TG_OP normally. This comment avoids writing a lone percent
-- character anywhere in this file for the same reason.
CREATE FUNCTION raw.forbid_payload_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'raw.payload is append-only: %% is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER payload_append_only
    BEFORE UPDATE OR DELETE ON raw.payload
    FOR EACH ROW EXECUTE FUNCTION raw.forbid_payload_mutation();
