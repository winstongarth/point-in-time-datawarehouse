# CLAUDE.md — Bitemporal Financial Data Warehouse

## 0. What this project is

A point-in-time correct financial data warehouse that ingests the same securities from
multiple public vendors, stores every fact with **two time dimensions**, reconciles vendors
against each other, and can reconstruct exactly what was knowable on any past date.

The capstone deliverable is a measured demonstration: the same factor backtest run twice —
once on point-in-time data, once on latest-restated data — with the performance gap quantified.

This is a portfolio project targeting systematic data platform / data operations roles at
quantitative investment firms. **Correctness and documentation matter more than breadth.**

---

## 1. Core concepts (read before writing code)

### Bitemporality

Every fact carries two independent time axes:

| Axis | Column(s) | Meaning |
|---|---|---|
| **Valid time** | `period_start`, `period_end` | The real-world period the fact describes. "Q2 2024 revenue" has valid time 2024-04-01 → 2024-06-30. |
| **Knowledge time** | `knowledge_from`, `knowledge_to` | The window during which our system believed this value. Opens when the vendor published it; closes when superseded. |

A restatement does **not** update a row. It closes the old row's `knowledge_to` and inserts a
new row with the same valid time and a later `knowledge_from`. Rows are never deleted or
overwritten in `core`.

### The three restatement sources this project must handle

1. **Amended filings** — a 10-K/A restates a prior quarter. EDGAR exposes this because each
   datapoint carries its own `filed` date and accession number.
2. **Retroactive price adjustment** — after a split or dividend, a vendor's adjusted-close
   history for *past* dates silently changes. The value for 2024-01-02 is not the same today
   as it was six months ago.
3. **Vendor backfill/correction** — a vendor reissues history with no announcement. Only
   detectable by hashing what you received and comparing to what you received before.

### Availability lag

`knowledge_from` is **not** the filing timestamp. A filing landing at 16:45 ET is not tradeable
that day. Every source declares a configurable `availability_lag` (default: next trading day
open) applied when computing `knowledge_from`. Do not hardcode this — it is a per-source config.

---

## 2. Hard scope limits

These exist to keep the project finishable. Do not expand them without an explicit instruction.

- **Universe: 50 tickers.** Large-cap US, defined in `config/universe.yaml`. Fixed list, not
  index-derived.
- **Fundamental metrics: 6.** revenue, net income, total assets, total equity, shares
  outstanding (diluted weighted average), operating cash flow.
- **History: 10 years** or vendor availability, whichever is shorter.
- **Frequency: daily** for prices, **quarterly** for fundamentals.
- **No intraday, no options, no international, no alternative data.**

### Anti-goals

- Not a backtesting framework. The backtest in Milestone 7 is deliberately crude — it exists
  only as an instrument to measure the data quality delta.
- Not a general ingestion framework. Three concrete adapters, one shared interface.
- No web UI. CLI plus generated markdown/HTML reports.
- No Kubernetes, no cloud deployment, no Docker Compose sprawl beyond Postgres.
- No ML.

---

## 3. Stack

| Concern | Choice | Note |
|---|---|---|
| Language | Python 3.11+ | |
| Package manager | `uv` | |
| Database | PostgreSQL 15+ | Local, via Docker. Uses `tstzrange`, `jsonb`, `numeric`. |
| DB access | `psycopg` 3 + hand-written SQL | **No ORM.** SQL is a demonstrated skill here — do not hide it. |
| Migrations | Alembic (SQL-only revisions) | |
| Dataframes | `polars` | pandas permitted only where a library forces it |
| CLI | `typer` | |
| Config | `pydantic-settings` + YAML | |
| Testing | `pytest` | |
| Lint/format/types | `ruff`, `mypy --strict` on `src/` | |
| Orchestration | Makefile + cron initially | Prefect only at Milestone 8, and only if it earns its place |

**Do not add a dependency without asking.**

---

## 4. Data sources

### 4.1 SEC EDGAR — fundamentals (primary)

- Company facts: `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` (CIK zero-padded to 10)
- Ticker→CIK map: `https://www.sec.gov/files/company_tickers.json`
- **A descriptive `User-Agent` header with a contact email is mandatory.** Requests without it
  are blocked. Set it from config; never commit a real email — use an env var.
- Rate limit: stay at or below 10 requests/second. Implement a token-bucket limiter.

Each datapoint carries `end`, `val`, `accn`, `fy`, `fp`, `form`, `filed`, and (for duration
concepts) `start`. `filed` is what makes point-in-time reconstruction possible — verify this
field's presence on first fetch and fail loudly if the API shape differs from expectations.

**Normalization is the hard part.** A single logical metric maps to multiple XBRL tags that
change across years and filers (e.g. revenue may appear as
`RevenueFromContractWithCustomerExcludingAssessedTax`, `Revenues`, or `SalesRevenueNet`).
Maintain an explicit priority-ordered mapping in `config/metric_map.yaml`:

```yaml
revenue:
  unit: USD
  tags:
    - RevenueFromContractWithCustomerExcludingAssessedTax
    - RevenueFromContractWithCustomerIncludingAssessedTax
    - Revenues
    - SalesRevenueNet
```

Record which tag was actually used in `vendor_native_tag` on every fact row. When a filer
switches tags mid-history, that must be visible in the data, not smoothed over.

### 4.2 yfinance — prices (primary)

Provides both `Close` and `Adj Close`. The divergence between them across fetch dates is the
mechanism for demonstrating retroactive adjustment. Unofficial and fragile — keep it strictly
behind the `PriceSource` interface so it can be swapped.

### 4.3 Tiingo — prices (secondary, for reconciliation)

**Amended at M2.** The spec originally named Stooq's CSV endpoint
(`https://stooq.com/q/d/l/?s=aapl.us&i=d`) here. As of the M2 build, that endpoint sits behind
a site-wide JavaScript proof-of-work bot challenge (a SHA-256 nonce search plus a `/__verify`
callback), confirmed with multiple User-Agent strings — it is not a header/politeness issue,
it is a deliberate anti-bot gate that a plain HTTP client cannot pass. Building a solver for
that challenge would be automated bot-detection evasion against a control the vendor put there
on purpose, so this project does not do that. See `docs/limitations.md`.

Replaced with **Tiingo**'s free-tier EOD prices API:
`https://api.tiingo.com/tiingo/daily/{ticker}/prices?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD&token=API_TOKEN`.
Requires a free account and API token (`PDW_TIINGO_API_TOKEN`, set via `.env`, never committed).
Each row includes both `close` and `adjClose`, so — like yfinance — the divergence between them
across fetch dates is available directly. Used only as an independent opinion to reconcile
against yfinance; expect it to disagree on some days, which is a feature of this project, not a
bug to eliminate.

---

## 5. Schema

Five schemas, strict one-way flow: `raw` → `stg` → `core`, with `dq` and `ops` observing.

### `raw` — immutable landing zone

```sql
raw.payload (
  payload_id     bigserial primary key,
  source         text        not null,
  endpoint       text        not null,
  request_params jsonb       not null,
  fetched_at     timestamptz not null,
  http_status    int         not null,
  content_sha256 char(64)    not null,
  body           bytea       not null,
  run_id         bigint      not null references ops.pipeline_run(run_id)
);
create index on raw.payload (source, content_sha256);
```

**Never mutate or delete `raw.payload`.** This table is the audit trail — every `core` fact
must be traceable back to a byte-identical vendor response. If `content_sha256` matches the
previous fetch for the same request, skip reparsing but still record the fetch (proof of
no-change is itself information).

### `stg` — parsed, typed, not yet deduplicated

One table per source shape. Truncated and rebuilt per run. No constraints beyond types.

### `core` — bitemporal facts

```sql
core.entity (
  entity_id  serial primary key,
  cik        char(10) not null unique,
  name       text     not null
);

-- Ticker↔entity mapping is itself bitemporal: tickers get reassigned.
core.entity_ticker (
  entity_id      int  not null references core.entity,
  ticker         text not null,
  knowledge_from timestamptz not null,
  knowledge_to   timestamptz not null default 'infinity'
);

core.fundamental_fact (
  fact_id           bigserial primary key,
  entity_id         int         not null references core.entity,
  metric_code       text        not null,
  period_start      date,
  period_end        date        not null,
  fiscal_year       int,
  fiscal_period     text,
  value             numeric      not null,
  unit              text         not null,
  source            text         not null,
  vendor_native_tag text,
  form_type         text,
  accession_no      text,
  filed_date        date         not null,
  knowledge_from    timestamptz  not null,
  knowledge_to      timestamptz  not null default 'infinity',
  supersedes        bigint       references core.fundamental_fact(fact_id),
  payload_id        bigint       not null references raw.payload,
  ingested_at       timestamptz  not null default now()
);

core.price_fact (
  entity_id      int         not null references core.entity,
  trade_date     date        not null,
  open, high, low, close  numeric,
  volume         bigint,
  adj_close      numeric,
  source         text        not null,
  knowledge_from timestamptz not null,
  knowledge_to   timestamptz not null default 'infinity',
  payload_id     bigint      not null references raw.payload
);
```

### Non-negotiable invariants

Enforce these as database constraints and as `pytest` assertions over the live database:

1. **No knowledge-time overlap.** For any `(entity_id, metric_code, period_end, source)`, the
   `[knowledge_from, knowledge_to)` ranges must not overlap. Enforce with a `tstzrange`
   `EXCLUDE USING gist` constraint — not application logic.
2. **Exactly one open row.** At most one row per key has `knowledge_to = 'infinity'`.
3. **`knowledge_from` ≥ `filed_date` + `availability_lag`.** Check constraint.
4. **`knowledge_from` < `knowledge_to`.** Check constraint.
5. **Full lineage.** Every `core` row has a non-null `payload_id`. Enforced by FK + `NOT NULL`.
6. **`raw` is append-only.** Enforce with a `BEFORE UPDATE OR DELETE` trigger that raises.

Invariant 1 is the heart of the project. If it can be violated, the warehouse is worthless.

### `dq` and `ops`

```sql
dq.check_result (check_id, check_name, dataset, run_id, severity, status,
                 observed jsonb, expected jsonb, evaluated_at)
dq.exception    (exception_id, check_id, entity_id, dimension_key, severity,
                 status, opened_at, closed_at, resolution_note)
ops.pipeline_run (run_id, pipeline, started_at, ended_at, status,
                  rows_in, rows_out, error jsonb)
```

Severity: `INFO` / `WARN` / `BREAK`. `BREAK` means the data is not fit for use and must block
promotion from `stg` to `core`.

---

## 6. The point-in-time read API

The only sanctioned way to read `core`. Direct `SELECT` against `core` in analysis code is
a bug.

```python
class PointInTimeReader:
    def __init__(self, conn, as_of: datetime): ...

    def fundamentals(self, metrics: list[str], tickers: list[str] | None = None) -> pl.DataFrame: ...
    def prices(self, tickers: list[str], start: date, end: date) -> pl.DataFrame: ...
    def latest(self) -> "PointInTimeReader":  # as_of = now; for the contrast experiment
        ...
```

Underlying predicate, applied uniformly:

```sql
WHERE knowledge_from <= :as_of AND knowledge_to > :as_of
```

Two guardrails:

- `as_of` must be timezone-aware. Reject naive datetimes at construction.
- The reader must **never** return a row whose `filed_date` is after `as_of`. Assert this in
  the reader itself, belt-and-braces alongside the DB constraint.

---

## 7. Reconciliation engine

Cross-vendor comparison producing `dq.exception` rows. Rules declared in
`config/reconciliation.yaml`, not code:

```yaml
- name: price_close_cross_vendor
  left:  {source: yfinance, field: close}
  right: {source: stooq,    field: close}
  grain: [entity_id, trade_date]
  tolerance: {type: relative, value: 0.001}
  severity: WARN
  escalate_if: {consecutive_days: 3, severity: BREAK}
```

Required checks:

| Check | Type | Severity |
|---|---|---|
| Cross-vendor close price within tolerance | cross-source | WARN → BREAK |
| Balance sheet identity: assets ≈ liabilities + equity | intra-record | BREAK |
| Revenue sign and magnitude sanity vs trailing median | statistical | WARN |
| Fundamental period coverage gaps (missing quarter) | completeness | WARN |
| Price staleness vs NYSE calendar | timeliness | BREAK |
| Volume/return outliers (z-score vs 250d window) | statistical | INFO |
| Payload hash unchanged when change was expected | freshness | WARN |
| XBRL tag switched for an entity mid-history | metadata | INFO |

Each check writes a result row every run — including passes. A check that only records
failures cannot support a coverage metric.

---

## 8. Milestones

Each milestone ends in something runnable and testable. Do not begin the next until the
acceptance criteria pass. Commit at each milestone boundary.

**M1 — Foundation.** Repo layout, `uv` project, Docker Postgres, Alembic wired, `ops.pipeline_run`,
structured JSON logging, `typer` CLI skeleton, CI running ruff + mypy + pytest.
*Accept:* `make up && make migrate && pdw --help` works from a clean clone.

**M2 — Raw ingestion.** EDGAR + yfinance + Stooq adapters behind a common `Source` interface.
Rate limiting, retry with backoff, `raw.payload` writes, append-only trigger, dedup by content hash.
*Accept:* `pdw ingest --source edgar --universe config/universe.yaml` populates `raw.payload` for
50 tickers; a second immediate run adds fetch records but zero new distinct hashes.

**M3 — Parse and normalize.** XBRL → `stg`, metric map applied, units validated, entity and
bitemporal ticker mapping built.
*Accept:* all 6 metrics present for ≥90% of expected entity-quarters; a coverage report names
every gap; `vendor_native_tag` populated on every row.

**Amended at M3.** Live against the full 50-ticker universe, coverage landed at 87.6%, not
≥90%, after three verified metric-map fixes (tag-switch-mid-history handling, an ABBV-style
net-income variant, pre-ASC-606 goods/services revenue tags — see `config/metric_map.yaml`'s
own comments). The remaining gap was diagnosed, not patched over: bank holding companies
(WFC, JPM, BAC, ...) have no unified GAAP "Revenue" concept at all, and a handful of large-caps
(BRK.B, GOOGL) genuinely have no XBRL fact for diluted shares outstanding for most of their
history under any tag in any taxonomy namespace present in their companyfacts response. Adding
more tags to close this gap further would mean guessing at unverified proxies, which is worse
than an honest, documented shortfall. Accepted at 87.6% with the cause named, per user sign-off
2026-07-20 — see `docs/limitations.md`.

**M4 — Bitemporal core loader.** The centerpiece. Promotes `stg` → `core` with correct
knowledge-time handling: new facts open a window, restatements close the prior window and open a
successor with `supersedes` set, unchanged facts are no-ops.
*Accept:* all six invariants hold under `pytest`; the loader is idempotent (second run inserts
zero rows); a synthetic amended-filing fixture produces exactly two rows with contiguous,
non-overlapping knowledge ranges.

**M5 — Point-in-time reader.** `PointInTimeReader` + `pdw query --as-of`.
*Accept:* for an entity with a known restatement, `as_of` before the amendment returns the
original value and after returns the restated one. Property test: no returned row ever has
`filed_date > as_of`, across randomized `as_of` samples.

**M6 — Quality and reconciliation.** All eight checks, exception lifecycle (open → triage →
close), coverage and profiling reports, auto-generated data dictionary written to
`docs/dictionary/` from live schema + config.
*Accept:* `pdw dq run` emits results for every check; seeded corruptions (nulled prices, shifted
decimal, dropped quarter, stale feed) are each detected with the correct severity; the data
dictionary regenerates deterministically and is committed.

**M7 — The experiment.** Naive quarterly-rebalanced earnings-yield long/short over the 50-name
universe, run twice: once through `PointInTimeReader(as_of=rebalance_date)`, once through
`.latest()`. Report cumulative return, Sharpe, turnover, and the number of positions that
differ per rebalance because of restatement.
*Accept:* `docs/findings.md` contains the comparison table, a chart of both equity curves, and
at least three named, traced case studies where restatement changed a position — each linked
to a specific `fact_id` and accession number.

**M8 — Operations layer.** SLA definitions per feed, freshness monitoring, a dependency DAG
showing blast radius of a feed failure, three written post-mortems from real failures
encountered during the build. Prefect only if the Makefile has demonstrably run out of road.
*Accept:* `pdw ops status` shows per-feed freshness against SLA; `docs/runbook.md` gives triage
steps for each `BREAK` severity check.

---

## 9. Testing

- **Unit tests use recorded fixtures, never the network.** Save real payloads once to
  `tests/fixtures/`, redact contact details, commit them. Any test hitting a live API must be
  marked `@pytest.mark.integration` and excluded from the default run.
- **Bitemporal logic gets synthetic fixtures.** Hand-construct restatement scenarios: a simple
  amendment, a double amendment, an out-of-order arrival (later filing ingested first), and a
  no-change re-fetch. Real data will not reliably contain all four.
- **Invariants are tested against the live database**, not just in application code.
- Target ≥85% coverage on `src/core/` and `src/quality/`. Adapters may be lower.

---

## 10. Documentation (a deliverable, not an afterthought)

The reviewer of this project may read only the docs. Write accordingly.

- `README.md` — what this is, why point-in-time matters, headline result from M7, quickstart.
- `docs/architecture.md` — schema flow, bitemporal model, sequence diagram of a restatement.
- `docs/dictionary/` — **auto-generated** per-dataset dictionaries. Field, type, source, nullability,
  transformation applied, known caveats. Committed so schema drift appears as a git diff.
- `docs/findings.md` — the M7 experiment with numbers and traced case studies.
- `docs/runbook.md` — triage procedure per check, escalation path, common failure modes.
- `docs/limitations.md` — **required.** State plainly: the universe is survivorship-biased
  (constructed from currently-listed names); free vendors offer no delivery SLA; the SEC
  ticker map is current-state-only so historical ticker changes are partially reconstructed.
  Naming these earns more credibility than silently ignoring them.

---

## 11. Working conventions for Claude Code

- **Ask before deviating from this spec.** If a design here proves wrong once you see the real
  data, say so and propose an amendment — do not silently work around it.
- **Do not add dependencies without asking.**
- **Do not weaken an invariant to make a test pass.** If an invariant is genuinely wrong,
  raise it explicitly. Constraints are the point of this project.
- One milestone per branch. Conventional commits. Every commit leaves the tree runnable.
- Write the SQL by hand. Verbose, commented, readable SQL is a deliverable here.
- No secrets in the repo. `.env` for the EDGAR contact email; commit `.env.example` only.
- When a vendor's actual response shape contradicts an assumption in this document, fix the
  document in the same commit as the code.
- Prefer failing loudly to coercing bad data. A pipeline that silently drops malformed rows is
  the exact failure mode this project exists to argue against.
