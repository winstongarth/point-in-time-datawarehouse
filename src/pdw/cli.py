from __future__ import annotations

from importlib.metadata import version as _package_version
from pathlib import Path
from typing import Annotated

import typer

from pdw.config import get_settings
from pdw.logging import configure_logging

app = typer.Typer(
    name="pdw",
    help="Point-in-time correct financial data warehouse.",
    no_args_is_help=True,
)


@app.callback()
def bootstrap() -> None:
    """Configure structured logging before any subcommand runs."""
    configure_logging(get_settings().log_level)


@app.command()
def version() -> None:
    """Print the installed pdw version."""
    typer.echo(_package_version("pdw"))


@app.command()
def ingest(
    source: Annotated[
        str, typer.Option("--source", help="Vendor adapter: edgar | yfinance | tiingo")
    ],
    universe: Annotated[
        Path, typer.Option("--universe", help="Path to the universe YAML file")
    ] = Path("config/universe.yaml"),
) -> None:
    """Fetch raw vendor data for the universe into raw.payload."""
    from pdw.db import get_connection
    from pdw.ingest import build_source, load_universe
    from pdw.ingest import ingest as run_ingest

    tickers = load_universe(universe)
    adapter = build_source(source)
    with get_connection() as conn:
        run_ingest(conn, adapter, tickers)


@app.command()
def parse(
    universe: Annotated[
        Path, typer.Option("--universe", help="Path to the universe YAML file")
    ] = Path("config/universe.yaml"),
    metric_map: Annotated[
        Path, typer.Option("--metric-map", help="Path to the metric map YAML file")
    ] = Path("config/metric_map.yaml"),
    report: Annotated[
        Path, typer.Option("--report", help="Where to write the coverage report")
    ] = Path("reports/coverage_report.md"),
) -> None:
    """Parse the latest EDGAR raw payloads into stg and core.entity/entity_ticker."""
    from pdw.coverage import compute_coverage, render_coverage_report
    from pdw.db import get_connection
    from pdw.ingest import load_universe
    from pdw.metric_map import load_metric_map
    from pdw.parse import run_parse

    tickers = load_universe(universe)
    mapping = load_metric_map(metric_map)

    with get_connection() as conn:
        summary, facts, ticker_by_cik = run_parse(conn, mapping, tickers)

    coverage = compute_coverage(facts, ticker_by_cik, frozenset(mapping))
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_coverage_report(coverage))

    typer.echo(
        f"parsed {summary.entities_parsed} entities, {summary.facts_written} facts; "
        f"coverage {coverage.coverage_pct:.1f}% ({coverage.fully_covered}/"
        f"{coverage.total_entity_quarters} entity-quarters); report at {report}"
    )
    if summary.tickers_without_cik:
        typer.echo(f"no CIK found for: {', '.join(summary.tickers_without_cik)}")
    if summary.tickers_without_companyfacts:
        typer.echo(
            f"no companyfacts payload for: {', '.join(summary.tickers_without_companyfacts)}"
        )


@app.command(name="load-fundamentals")
def load_fundamentals_command(
    sources_config: Annotated[
        Path, typer.Option("--sources-config", help="Path to the source-availability YAML file")
    ] = Path("config/sources.yaml"),
) -> None:
    """Promote stg.edgar_fundamental_fact into the bitemporal core.fundamental_fact."""
    from pdw.availability import load_source_availability
    from pdw.db import get_connection
    from pdw.load_fundamentals import load_fundamentals

    availability = load_source_availability(sources_config)["edgar"]
    with get_connection() as conn:
        summary = load_fundamentals(conn, availability)

    typer.echo(
        f"processed {summary.keys_processed} (entity, metric, period) keys; "
        f"inserted {summary.rows_inserted} new fact rows, "
        f"relinked {summary.rows_relinked} existing rows"
    )


@app.command(name="load-prices")
def load_prices_command(
    source: Annotated[str, typer.Option("--source", help="yfinance | tiingo")],
    sources_config: Annotated[
        Path, typer.Option("--sources-config", help="Path to the source-availability YAML file")
    ] = Path("config/sources.yaml"),
) -> None:
    """Promote raw price payloads for `source` into the bitemporal core.price_fact."""
    from pdw.availability import load_source_availability
    from pdw.db import get_connection
    from pdw.load_prices import load_prices

    availability = load_source_availability(sources_config)[source]
    with get_connection() as conn:
        summary = load_prices(conn, source, availability)

    typer.echo(
        f"processed {summary.keys_processed} tickers; "
        f"inserted {summary.rows_inserted} new fact rows, "
        f"relinked {summary.rows_relinked} existing rows"
    )
