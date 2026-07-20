# pdw — Point-in-time Data Warehouse

A bitemporal financial data warehouse. It ingests the same 50 large-cap US
securities from multiple public vendors, stores every fact with **two
independent time dimensions**, reconciles vendors against each other, and can
reconstruct exactly what was knowable on any past date — not just what is
true today.

This is a portfolio project for systematic data platform / data operations
roles at quantitative investment firms. Correctness and documentation matter
more than breadth: the scope is deliberately narrow (50 tickers, 6
fundamental metrics, 10 years of history, daily/quarterly frequency — see
[Scope and limitations](#scope-and-limitations)) so that what's here is
demonstrably correct rather than broad and unverified.

## Table of contents

- [Why point-in-time correctness matters](#why-point-in-time-correctness-matters)
- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [Data sources](#data-sources)
- [Project status / milestone roadmap](#project-status--milestone-roadmap)
- [Quickstart](#quickstart)
- [Common tasks](#common-tasks)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Documentation](#documentation)
- [Scope and limitations](#scope-and-limitations)

## Why point-in-time correctness matters

Financial datasets get restated, silently and often:

1. **Amended filings** — a 10-K/A restates a prior quarter's numbers. EDGAR
   exposes this because every datapoint carries its own `filed` date and
   accession number.
2. **Retroactive price adjustment** — after a split or dividend, a vendor's
   entire adjusted-close history for *past* dates changes, with no
   announcement. The adjusted close for 2024-01-02 is not the same value
   today as it was six months ago.
3. **Vendor backfill/correction** — a vendor reissues history with no
   announcement at all. The only way to detect it is to hash what you
   received and compare it to what you received before.

A warehouse that just `UPDATE`s a row when a new value arrives destroys the
ability to answer "what did we believe on date X?" — any backtest or
analysis re-run today silently uses information that didn't exist yet at the
time it claims to represent. That look-ahead bias is invisible unless you
design against it from the start.

This project stores both:

| Axis | Columns | Meaning |
|---|---|---|
| **Valid time** | `period_start`, `period_end` | The real-world period a fact describes. "Q2 2024 revenue" has valid time 2024-04-01 → 2024-06-30. |
| **Knowledge time** | `knowledge_from`, `knowledge_to` | The window during which the warehouse believed this value. Opens when the vendor published it (adjusted for [availability lag](#availability-lag)); closes when superseded. |

A restatement never updates a row — it closes the old row's `knowledge_to`
and inserts a new row with the same valid time and a later `knowledge_from`,
linked via `supersedes`. Concretely, for one company's Q2 2024 revenue after
a 10-Q/A amendment:

| `fact_id` | `period_end` | `value` | `knowledge_from` | `knowledge_to` | `supersedes` |
|---|---|---|---|---|---|
| 101 | 2024-06-30 | 24,930,000,000 | 2024-08-01T13:00Z | 2024-11-05T13:00Z | — |
| 187 | 2024-06-30 | 24,870,000,000 | 2024-11-05T13:00Z | infinity | 101 |

A query with `as_of = 2024-09-01` returns fact 101 (what was actually known
then); a query with `as_of = 2024-12-01` returns fact 187. Both are correct
answers to different questions, and both stay queryable forever — nothing is
ever deleted or overwritten in `core`.

The capstone deliverable ([Milestone 7](#project-status--milestone-roadmap))
runs a naive earnings-yield long/short backtest twice — once through
point-in-time data, once through latest-restated data — and quantifies the
performance gap this causes. That result will be summarized here once M7
lands; see [docs/findings.md](docs/findings.md) (created at M7).

### Availability lag

`knowledge_from` is **not** the filing timestamp. A filing that lands at
16:45 ET is not tradeable that day. Every source declares a configurable
`availability_lag` (default: next trading day open) applied when computing
`knowledge_from` — this is per-source config, not a hardcoded constant.

## Architecture

Five Postgres schemas, with a strict one-way data flow:

```mermaid
flowchart LR
    EDGAR[SEC EDGAR] --> RAW
    YF[yfinance] --> RAW
    STQ[Stooq] --> RAW

    RAW[("raw\n(immutable landing zone)")] --> STG[("stg\n(parsed, typed)")]
    STG --> CORE[("core\n(bitemporal facts)")]

    CORE --> PITR["PointInTimeReader\n(the only sanctioned read path)"]
    STG -.->|reconciliation| DQ[("dq\n(check_result, exception)")]
    CORE -.->|reconciliation| DQ

    RAW -.-> OPS[("ops.pipeline_run\n(lineage for every run)")]
    STG -.-> OPS
    CORE -.-> OPS
```

- **`raw`** — byte-identical vendor responses, hashed and append-only.
  Enforced by a `BEFORE UPDATE OR DELETE` trigger that raises. Every `core`
  fact traces back to a `payload_id` here.
- **`stg`** — parsed and typed, truncated and rebuilt per run. No
  constraints beyond types.
- **`core`** — the bitemporal facts (`fundamental_fact`, `price_fact`,
  `entity`, `entity_ticker`). Six invariants are enforced as **database
  constraints**, not application logic — most importantly, an `EXCLUDE USING
  gist` constraint on `tstzrange(knowledge_from, knowledge_to)` per
  `(entity_id, metric_code, period_end, source)`, so knowledge-time overlap
  is structurally impossible.
- **`dq`** — cross-vendor reconciliation results and an exception lifecycle
  (open → triage → close), declared in `config/reconciliation.yaml`, not
  code.
- **`ops`** — `pipeline_run` lineage: every `raw`/`dq` row traces back to the
  exact run that produced it.

The only sanctioned way to read `core` is `PointInTimeReader`:

```python
class PointInTimeReader:
    def __init__(self, conn, as_of: datetime): ...   # as_of must be tz-aware
    def fundamentals(self, metrics, tickers=None) -> pl.DataFrame: ...
    def prices(self, tickers, start, end) -> pl.DataFrame: ...
    def latest(self) -> "PointInTimeReader": ...       # as_of = now
```

applying `WHERE knowledge_from <= :as_of AND knowledge_to > :as_of`
uniformly, with an in-code assertion that it never returns a row whose
`filed_date` is after `as_of` — belt-and-braces alongside the DB constraint.
Direct `SELECT` against `core` from analysis code is considered a bug.

Full schema DDL and rationale: [docs/architecture.md](docs/architecture.md)
(from M4 onward) and [CLAUDE.md](CLAUDE.md#5-schema).

## Tech stack

| Concern | Choice | Note |
|---|---|---|
| Language | Python 3.11+ | |
| Package manager | [`uv`](https://docs.astral.sh/uv/) | |
| Database | PostgreSQL 15+ | Local, via Docker. Uses `tstzrange`, `jsonb`, `numeric`. |
| DB access | `psycopg` 3 + hand-written SQL | No ORM — SQL is a demonstrated skill here. |
| Migrations | Alembic (SQL-only revisions) | Hand-written SQL, versioned in `migrations/sql/`. |
| Dataframes | `polars` | pandas only where a library forces it. |
| CLI | `typer` | |
| Config | `pydantic-settings` + YAML | |
| Testing | `pytest` | |
| Lint/format/types | `ruff`, `mypy --strict` on `src/` | |
| Orchestration | Makefile + cron initially | Prefect only if it earns its place at M8. |

## Data sources

| Source | Role | Notes |
|---|---|---|
| [SEC EDGAR](https://www.sec.gov/edgar/sec-api-documentation) `companyfacts` API | Fundamentals (primary) | Requires a descriptive `User-Agent` with a contact email; rate-limited to ≤10 req/s via a token-bucket limiter. `filed` date on every datapoint is what makes point-in-time reconstruction possible. |
| [yfinance](https://github.com/ranaroussi/yfinance) | Prices (primary) | Unofficial and fragile — kept strictly behind a `PriceSource` interface. `Close` vs `Adj Close` divergence across fetch dates is the mechanism for demonstrating retroactive adjustment. |
| [Stooq](https://stooq.com/) | Prices (secondary) | Independent opinion for cross-vendor reconciliation only. Expected to disagree with yfinance on some days — that disagreement is a feature, not a bug. |

A single logical metric can map to multiple XBRL tags across years and
filers (e.g. revenue as `RevenueFromContractWithCustomerExcludingAssessedTax`,
`Revenues`, or `SalesRevenueNet`). The mapping is an explicit,
priority-ordered list in `config/metric_map.yaml` (from M2), and whichever
tag was actually used is recorded in `vendor_native_tag` on every fact row —
tag switches are visible in the data, never smoothed over.

## Project status / milestone roadmap

Each milestone ends in something runnable and testable, gets its own branch,
and is committed once its accept criteria pass — the next milestone doesn't
start until then.

| # | Milestone | Accept criteria | Status |
|---|---|---|---|
| M1 | Foundation — repo layout, `uv` project, Docker Postgres, Alembic, `ops.pipeline_run`, structured JSON logging, `typer` CLI skeleton, CI | `make up && make migrate && pdw --help` works from a clean clone | ✅ Done |
| M2 | Raw ingestion — EDGAR + yfinance + Stooq adapters, rate limiting, retry, content-hash dedup | `pdw ingest --source edgar ...` populates `raw.payload` for 50 tickers; a second run adds fetch records but zero new hashes | ⬜ Planned |
| M3 | Parse and normalize — XBRL → `stg`, metric map applied, entity/ticker mapping built | All 6 metrics present for ≥90% of expected entity-quarters; coverage report; `vendor_native_tag` populated everywhere | ⬜ Planned |
| M4 | Bitemporal core loader — `stg` → `core` with correct knowledge-time handling | All 6 invariants hold under `pytest`; loader is idempotent; synthetic amendment fixture produces exactly 2 non-overlapping rows | ⬜ Planned |
| M5 | Point-in-time reader — `PointInTimeReader` + `pdw query --as-of` | `as_of` before/after a known restatement returns original/restated value; property test: no row ever has `filed_date > as_of` | ⬜ Planned |
| M6 | Quality and reconciliation — all 8 checks, exception lifecycle, auto-generated data dictionary | `pdw dq run` emits results for every check; seeded corruptions detected at correct severity; dictionary regenerates deterministically | ⬜ Planned |
| M7 | The experiment — earnings-yield long/short, point-in-time vs. latest | `docs/findings.md` has the comparison table, equity-curve chart, and ≥3 traced case studies linked to `fact_id`/accession number | ⬜ Planned |
| M8 | Operations layer — SLA/freshness monitoring, dependency DAG, post-mortems | `pdw ops status` shows per-feed freshness; `docs/runbook.md` gives triage steps per `BREAK` check | ⬜ Planned |

Full milestone detail: [CLAUDE.md](CLAUDE.md#8-milestones).

## Quickstart

Prerequisites: [`uv`](https://docs.astral.sh/uv/), Docker Desktop, `make`.

```sh
git clone <this repo>
cd project-etl
cp .env.example .env       # defaults already match docker-compose.yml
uv sync

make up                     # start Postgres 15 in Docker (published on host port 5433)
make migrate                 # apply Alembic migrations

uv run pdw --help
```

`PDW_DATABASE_URL` defaults to `postgresql://pdw:pdw@localhost:5433/pdw` —
port **5433**, not the Postgres default 5432, specifically so this doesn't
collide with a native Postgres install some machines already have running.
If 5433 is also taken on your machine, change the published port in
`docker-compose.yml` and `PDW_DATABASE_URL` in `.env` to match.

Once `pdw` is installed into an activated environment, the `uv run` prefix
above is optional — `pdw --help` works directly.

## Common tasks

| Command | What it does |
|---|---|
| `make up` / `make down` | Start / stop the local Postgres container |
| `make logs` | Tail the Postgres container's logs |
| `make migrate` / `make downgrade` | Apply / roll back one Alembic revision |
| `make lint` | `ruff check .` |
| `make format` | `ruff format .` |
| `make typecheck` | `mypy --strict` on `src/` |
| `make test` | `pytest` (network-hitting tests are marked `integration` and excluded by default) |
| `make check` | lint + typecheck + test, in that order |

## Project layout

```
src/pdw/        application code (CLI, config, logging, ...)
migrations/     Alembic env.py + hand-written SQL-only revisions (migrations/sql/)
tests/          pytest; fixtures under tests/fixtures/ (from M2 onward)
config/         universe, metric mapping, reconciliation rules (from M2 onward)
docs/           architecture, data dictionary, findings, runbook, limitations
CLAUDE.md       full project spec: schema, invariants, milestones, conventions
```

## Testing

- **Unit tests use recorded fixtures, never the network.** Real payloads are
  saved once to `tests/fixtures/`, contact details redacted, and committed.
  Anything hitting a live API is marked `@pytest.mark.integration` and
  excluded from the default run (see `addopts` in `pyproject.toml`).
- **Bitemporal logic gets synthetic fixtures** (from M4): a simple amendment,
  a double amendment, an out-of-order arrival, and a no-change re-fetch —
  real data won't reliably contain all four.
- **Invariants are tested against the live database**, not just asserted in
  application code.
- Target ≥85% coverage on `src/core/` and `src/quality/` once they exist;
  adapters may be lower.

## Documentation

The reviewer of this project may read only the docs — they're written
accordingly, not as an afterthought:

| Doc | Contents | Lands at |
|---|---|---|
| [docs/architecture.md](docs/architecture.md) | Schema flow, bitemporal model, sequence diagram of a restatement | M4 |
| [docs/dictionary/](docs/dictionary/) | Auto-generated per-dataset field/type/source/nullability/caveat reference, regenerated from live schema + config | M6 |
| [docs/findings.md](docs/findings.md) | The M7 experiment: numbers, equity curves, traced case studies | M7 |
| [docs/runbook.md](docs/runbook.md) | Triage procedure and escalation path per `dq` check | M8 |
| [docs/limitations.md](docs/limitations.md) | What this project deliberately does not attempt, stated plainly | Available now |

## Scope and limitations

Hard scope limits, kept fixed on purpose to keep this finishable: 50 tickers
(`config/universe.yaml`, a fixed list, not index-derived), 6 fundamental
metrics (revenue, net income, total assets, total equity, diluted weighted
average shares outstanding, operating cash flow), 10 years of history or
vendor availability (whichever is shorter), daily prices / quarterly
fundamentals, no intraday, no options, no international, no alternative
data, no ML, no web UI.

See [docs/limitations.md](docs/limitations.md) for what this means in
practice — survivorship bias in the universe, no vendor delivery SLA, and a
current-state-only SEC ticker map.
