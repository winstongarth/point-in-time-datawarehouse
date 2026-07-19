# pdw — Point-in-time Data Warehouse

A bitemporal financial data warehouse: it ingests the same 50 large-cap US
securities from multiple public vendors (SEC EDGAR, yfinance, Stooq), stores
every fact with **two time dimensions** (the period a fact describes, and the
window during which the warehouse believed it), reconciles vendors against
each other, and can reconstruct exactly what was knowable as of any past date.

## Why point-in-time matters

Financial datasets get restated: amended filings, retroactively adjusted
price history, silent vendor backfills. A naive warehouse overwrites the old
value, which means any analysis re-run today "sees the future" relative to
what was actually known at the time. This project measures that effect
directly — Milestone 7 runs the same simple backtest twice, once against
point-in-time data and once against latest-restated data, and quantifies the
performance gap. That result will be summarized here once M7 lands.

See [CLAUDE.md](CLAUDE.md) for the full project spec — schema, invariants,
milestone plan, and working conventions — and
[docs/limitations.md](docs/limitations.md) for what this project deliberately
does not attempt.

## Status

**Milestone 1 (Foundation)** — repo layout, `uv` project, Postgres via
Docker, Alembic migrations, structured JSON logging, `typer` CLI skeleton,
CI running ruff + mypy + pytest.

## Quickstart

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker Desktop.

```sh
git clone <this repo>
cd project-etl
cp .env.example .env       # defaults already match docker-compose.yml
uv sync

make up                    # start Postgres in Docker
make migrate                # apply Alembic migrations

uv run pdw --help
```

## Common tasks

| Command | What it does |
|---|---|
| `make up` / `make down` | Start / stop the local Postgres container |
| `make migrate` / `make downgrade` | Apply / roll back one Alembic revision |
| `make lint` | `ruff check .` |
| `make format` | `ruff format .` |
| `make typecheck` | `mypy --strict` on `src/` |
| `make test` | `pytest` (network-hitting tests are marked `integration` and excluded by default) |
| `make check` | lint + typecheck + test, in that order |

## Project layout

```
src/pdw/        application code (CLI, config, logging, ...)
migrations/     Alembic env + hand-written SQL-only revisions
tests/          pytest, fixtures under tests/fixtures/ (from M2 onward)
config/         universe, metric mapping, reconciliation rules (from M2 onward)
docs/           architecture, data dictionary, findings, runbook, limitations
```
