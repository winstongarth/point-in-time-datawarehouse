-- stg.edgar_fundamental_fact: parsed, typed EDGAR XBRL datapoints, with the
-- metric map (config/metric_map.yaml) already applied to pick which vendor
-- tag represents each canonical metric. "No constraints beyond types"
-- (CLAUDE.md 5) is deliberate: this table is truncated and rebuilt on every
-- parse run, duplicates are expected until promoted to core (M4), and
-- referential/uniqueness integrity is enforced there, not here.
CREATE TABLE stg.edgar_fundamental_fact (
    cik               char(10),
    entity_name       text,
    metric_code       text,
    period_start      date,
    period_end        date,
    fiscal_year       int,
    fiscal_period     text,
    value             numeric,
    unit              text,
    vendor_native_tag text,
    form_type         text,
    accession_no      text,
    filed_date        date,
    payload_id        bigint
);

-- core.entity / core.entity_ticker: reference data, not bitemporal facts,
-- but the ticker mapping genuinely is bitemporal (CLAUDE.md 5: "tickers get
-- reassigned"), so it gets the same non-overlap rigor as invariant 1 does
-- for fundamental_fact/price_fact in M4 - just applied to (ticker, knowledge
-- range) instead of (entity_id, metric_code, period_end, source).
CREATE TABLE core.entity (
    entity_id serial PRIMARY KEY,
    cik       char(10) NOT NULL UNIQUE,
    name      text     NOT NULL
);

CREATE TABLE core.entity_ticker (
    entity_id      int         NOT NULL REFERENCES core.entity (entity_id),
    ticker         text        NOT NULL,
    knowledge_from timestamptz NOT NULL,
    knowledge_to   timestamptz NOT NULL DEFAULT 'infinity',
    CONSTRAINT entity_ticker_knowledge_order_check
        CHECK (knowledge_from < knowledge_to)
);

-- EXCLUDE with a plain equality column requires btree_gist (it supplies the
-- "=" GiST operator class for text that tstzrange's "&&" needs alongside).
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- No ticker may point at two entities during overlapping knowledge windows.
ALTER TABLE core.entity_ticker
    ADD CONSTRAINT entity_ticker_no_overlap
    EXCLUDE USING gist (
        ticker WITH =,
        tstzrange(knowledge_from, knowledge_to) WITH &&
    );

CREATE INDEX entity_ticker_entity_idx ON core.entity_ticker (entity_id);
