-- core.fundamental_fact: the bitemporal promotion target for
-- stg.edgar_fundamental_fact. Every restatement closes the prior row's
-- knowledge_to and opens a successor with supersedes set; rows are never
-- deleted or overwritten.
CREATE TABLE core.fundamental_fact (
    fact_id           bigserial   PRIMARY KEY,
    entity_id         int         NOT NULL REFERENCES core.entity (entity_id),
    metric_code       text        NOT NULL,
    period_start      date,
    period_end        date        NOT NULL,
    fiscal_year       int,
    fiscal_period     text,
    value             numeric     NOT NULL,
    unit              text        NOT NULL,
    source            text        NOT NULL,
    vendor_native_tag text,
    form_type         text,
    accession_no      text,
    filed_date        date        NOT NULL,
    knowledge_from    timestamptz NOT NULL,
    knowledge_to      timestamptz NOT NULL DEFAULT 'infinity',
    supersedes        bigint      REFERENCES core.fundamental_fact (fact_id),
    payload_id        bigint      NOT NULL REFERENCES raw.payload (payload_id),
    ingested_at       timestamptz NOT NULL DEFAULT now(),

    -- Invariant 4: knowledge_from < knowledge_to.
    CONSTRAINT fundamental_fact_knowledge_order_check
        CHECK (knowledge_from < knowledge_to),

    -- Invariant 3: knowledge_from >= filed_date + availability_lag.
    -- The exact per-source lag (configurable, not hardcoded -
    -- see config/sources.yaml and pdw.availability) is an application-level
    -- concern computed at load time and covered by pytest, not something a
    -- static CHECK constraint can encode (it would need to know which
    -- source's config applies, and a full trading calendar). What the
    -- database CAN enforce unconditionally, for every source's lag, is the
    -- direction: knowledge can never precede the filing it came from.
    CONSTRAINT fundamental_fact_lag_check
        CHECK (knowledge_from >= filed_date::timestamptz)
);

-- Invariant 1 (the heart of the project): no knowledge-time overlap for a
-- given (entity_id, metric_code, period_start, period_end, source).
-- btree_gist supplies the "=" GiST operator classes EXCLUDE needs alongside
-- tstzrange's "&&". (Invariant 2, "at most one open row per key", falls out
-- of this for free: two rows both open to infinity necessarily overlap.)
--
-- period_start is in the key, not just period_end, because real filings
-- report more than one duration ending on the same date - e.g. a 10-Q's
-- "3 months ended June 30" and "6 months ended June 30" revenue figures
-- share period_end but are different, simultaneously-true facts, not one
-- restating the other (verified live: a Verizon 10-Q, accession
-- 0000732712-19-000052). period_start is
-- NULL for instant concepts (Assets, StockholdersEquity); coalesced to a
-- fixed sentinel here because Postgres's "=" never matches NULL to NULL,
-- which would otherwise silently let two genuinely colliding instant-concept
-- rows both through.
CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE core.fundamental_fact
    ADD CONSTRAINT fundamental_fact_no_overlap
    EXCLUDE USING gist (
        entity_id WITH =,
        metric_code WITH =,
        COALESCE(period_start, '0001-01-01'::date) WITH =,
        period_end WITH =,
        source WITH =,
        tstzrange(knowledge_from, knowledge_to) WITH &&
    );

CREATE INDEX fundamental_fact_entity_metric_idx
    ON core.fundamental_fact (entity_id, metric_code, period_start, period_end);

-- core.price_fact: same bitemporal treatment for daily prices. Adds a
-- surrogate key column beyond what an illustrative DDL might show, since
-- every other core table has one and the loader needs a stable id to set
-- as a later row's reference point when reconciling, so fact_id is added
-- here on the same pattern as fundamental_fact.
CREATE TABLE core.price_fact (
    fact_id        bigserial   PRIMARY KEY,
    entity_id      int         NOT NULL REFERENCES core.entity (entity_id),
    trade_date     date        NOT NULL,
    open           numeric,
    high           numeric,
    low            numeric,
    close          numeric,
    volume         bigint,
    adj_close      numeric,
    source         text        NOT NULL,
    knowledge_from timestamptz NOT NULL,
    knowledge_to   timestamptz NOT NULL DEFAULT 'infinity',
    payload_id     bigint      NOT NULL REFERENCES raw.payload (payload_id),

    CONSTRAINT price_fact_knowledge_order_check
        CHECK (knowledge_from < knowledge_to),

    -- price_fact has no filed_date; trade_date is the
    -- analogous basis a price's knowledge can never precede.
    CONSTRAINT price_fact_lag_check
        CHECK (knowledge_from >= trade_date::timestamptz)
);

ALTER TABLE core.price_fact
    ADD CONSTRAINT price_fact_no_overlap
    EXCLUDE USING gist (
        entity_id WITH =,
        trade_date WITH =,
        source WITH =,
        tstzrange(knowledge_from, knowledge_to) WITH &&
    );

CREATE INDEX price_fact_entity_date_idx ON core.price_fact (entity_id, trade_date);
