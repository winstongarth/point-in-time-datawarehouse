from __future__ import annotations

from datetime import date, datetime
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
    report.write_text(render_coverage_report(coverage), encoding="utf-8")

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


query_app = typer.Typer(help="Point-in-time reads against core (the only sanctioned read path).")
app.add_typer(query_app, name="query")


def _parse_as_of(as_of: str) -> datetime:
    parsed = datetime.fromisoformat(as_of)
    if parsed.tzinfo is None:
        raise typer.BadParameter(
            f"as_of must be timezone-aware, e.g. '2021-06-01T00:00:00+00:00' (got {as_of!r})"
        )
    return parsed


@query_app.command("fundamentals")
def query_fundamentals(
    as_of: Annotated[
        str, typer.Option("--as-of", help="Timezone-aware ISO 8601 timestamp")
    ],
    metrics: Annotated[
        str, typer.Option("--metrics", help="Comma-separated metric codes")
    ],
    tickers: Annotated[
        str | None, typer.Option("--tickers", help="Comma-separated tickers; omit for all")
    ] = None,
) -> None:
    """Fundamentals as they were known at --as-of."""
    import polars as pl

    from pdw.db import get_connection
    from pdw.query import PointInTimeReader

    parsed_as_of = _parse_as_of(as_of)
    metric_list = [m.strip() for m in metrics.split(",")]
    ticker_list = [t.strip() for t in tickers.split(",")] if tickers else None

    with get_connection() as conn:
        reader = PointInTimeReader(conn, parsed_as_of)
        df = reader.fundamentals(metric_list, ticker_list)

    with pl.Config(tbl_rows=-1):
        typer.echo(str(df))


@query_app.command("prices")
def query_prices(
    as_of: Annotated[
        str, typer.Option("--as-of", help="Timezone-aware ISO 8601 timestamp")
    ],
    tickers: Annotated[str, typer.Option("--tickers", help="Comma-separated tickers")],
    start: Annotated[str, typer.Option("--start", help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="YYYY-MM-DD")],
    source: Annotated[
        str | None,
        typer.Option(
            "--source",
            help="Vendor to filter to (yfinance is the primary price source); "
            "omit to see every source's row for a date, e.g. for cross-vendor comparison",
        ),
    ] = "yfinance",
) -> None:
    """Prices as they were known at --as-of."""
    import polars as pl

    from pdw.db import get_connection
    from pdw.query import PointInTimeReader

    parsed_as_of = _parse_as_of(as_of)
    ticker_list = [t.strip() for t in tickers.split(",")]

    with get_connection() as conn:
        reader = PointInTimeReader(conn, parsed_as_of)
        df = reader.prices(
            ticker_list, date.fromisoformat(start), date.fromisoformat(end), source=source
        )

    with pl.Config(tbl_rows=-1):
        typer.echo(str(df))


dq_app = typer.Typer(help="Data quality checks and exception triage.")
app.add_typer(dq_app, name="dq")


@dq_app.command("run")
def dq_run(
    reconciliation_config: Annotated[
        Path,
        typer.Option("--reconciliation-config", help="Path to the reconciliation rules YAML"),
    ] = Path("config/reconciliation.yaml"),
) -> None:
    """Run all 8 quality checks; writes dq.check_result and updates dq.exception."""
    from pdw.db import get_connection
    from pdw.dq_engine import run_all_checks

    with get_connection() as conn:
        summary = run_all_checks(conn, reconciliation_config)

    typer.echo(
        f"{summary.total_checks} check results: {summary.passed} passed, "
        f"{summary.failed} failed {summary.by_severity_fail or ''}"
    )


@dq_app.command("status")
def dq_status() -> None:
    """List currently open/in-triage exceptions."""
    from pdw.db import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ex.exception_id, cr.check_name, ex.dimension_key, ex.severity,
                   ex.status, ex.opened_at::text
            FROM dq.exception ex
            JOIN dq.check_result cr ON cr.check_id = ex.check_id
            WHERE ex.status IN ('open', 'triage')
            ORDER BY ex.severity DESC, ex.opened_at
            """
        )
        rows = cur.fetchall()

    if not rows:
        typer.echo("no open exceptions")
        return
    for exception_id, check_name, dimension_key, severity, status, opened_at in rows:
        typer.echo(
            f"[{exception_id}] {severity} {status} {check_name} {dimension_key} "
            f"(opened {opened_at})"
        )


@dq_app.command("triage")
def dq_triage(
    exception_id: Annotated[int, typer.Argument()],
    note: Annotated[str, typer.Option("--note")],
) -> None:
    """Move an open exception to triage."""
    from pdw.db import get_connection
    from pdw.dq_engine import triage_exception

    with get_connection() as conn:
        triage_exception(conn, exception_id, note)
    typer.echo(f"exception {exception_id} moved to triage")


@dq_app.command("resolve")
def dq_resolve(
    exception_id: Annotated[int, typer.Argument()],
    note: Annotated[str, typer.Option("--note")],
) -> None:
    """Manually close an open or in-triage exception."""
    from pdw.db import get_connection
    from pdw.dq_engine import resolve_exception

    with get_connection() as conn:
        resolve_exception(conn, exception_id, note)
    typer.echo(f"exception {exception_id} resolved")


dictionary_app = typer.Typer(help="Auto-generated data dictionary.")
app.add_typer(dictionary_app, name="dictionary")


@dictionary_app.command("generate")
def dictionary_generate(
    out_dir: Annotated[
        Path, typer.Option("--out", help="Output directory")
    ] = Path("docs/dictionary"),
) -> None:
    """Regenerate docs/dictionary/ from the live schema."""
    from pdw.db import get_connection
    from pdw.dictionary import generate_dictionary

    with get_connection() as conn:
        written = generate_dictionary(conn, out_dir)
    typer.echo(f"wrote {len(written)} dictionary files to {out_dir}")


backtest_app = typer.Typer(help="The M7 experiment: point-in-time vs. latest-restated backtest.")
app.add_typer(backtest_app, name="backtest")


@backtest_app.command("run")
def backtest_run(
    universe: Annotated[
        Path, typer.Option("--universe", help="Path to the universe YAML file")
    ] = Path("config/universe.yaml"),
    start: Annotated[
        str, typer.Option("--start", help="First rebalance date, YYYY-MM-DD")
    ] = "2017-01-01",
    end: Annotated[
        str, typer.Option("--end", help="Last rebalance date, YYYY-MM-DD")
    ] = "2026-07-01",
    out: Annotated[
        Path, typer.Option("--out", help="Where to write the findings report")
    ] = Path("docs/findings.md"),
    chart_out: Annotated[
        Path, typer.Option("--chart-out", help="Where to write the equity curve chart")
    ] = Path("docs/findings_equity_curve.svg"),
) -> None:
    """Run the naive earnings-yield long/short twice (point-in-time and
    latest) and write the comparison + case studies to --out."""
    from pdw.backtest import (
        compare_portfolios,
        equity_curve,
        find_case_studies,
        generate_rebalance_dates,
        render_equity_curve_svg,
        render_findings_report,
        run_backtest,
    )
    from pdw.db import get_connection
    from pdw.ingest import load_universe

    tickers = load_universe(universe)
    rebalance_dates = generate_rebalance_dates(date.fromisoformat(start), date.fromisoformat(end))

    with get_connection() as conn:
        pit_run = run_backtest(conn, tickers, rebalance_dates, "point_in_time")
        latest_run = run_backtest(conn, tickers, rebalance_dates, "latest")

    differences = compare_portfolios(pit_run, latest_run)
    case_studies = find_case_studies(differences, pit_run, latest_run)

    chart_out.parent.mkdir(parents=True, exist_ok=True)
    chart_out.write_text(
        render_equity_curve_svg(equity_curve(pit_run), equity_curve(latest_run)),
        encoding="utf-8",
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_findings_report(pit_run, latest_run, differences, case_studies, chart_out.name),
        encoding="utf-8",
    )

    typer.echo(
        f"{len(pit_run.portfolios)} rebalances, {len(differences)} position differences, "
        f"{len(case_studies)} case studies; report at {out}"
    )


ops_app = typer.Typer(help="Feed freshness monitoring and dependency blast radius.")
app.add_typer(ops_app, name="ops")


@ops_app.command("status")
def ops_status(
    sla_config: Annotated[
        Path, typer.Option("--sla-config", help="Path to the per-feed SLA YAML file")
    ] = Path("config/sla.yaml"),
) -> None:
    """Per-feed freshness against its SLA."""
    from pdw.db import get_connection
    from pdw.ops import compute_feed_status, load_sla

    sla_definitions = load_sla(sla_config)
    with get_connection() as conn:
        statuses = compute_feed_status(conn, sla_definitions)

    for status in statuses:
        if status.last_fetched_at is None:
            typer.echo(f"[{status.status.upper()}] {status.source}: never fetched")
        else:
            typer.echo(
                f"[{status.status.upper()}] {status.source}: last fetched "
                f"{status.last_fetched_at.isoformat()} ({status.staleness_days:.1f} days ago)"
            )


@ops_app.command("deps")
def ops_deps(
    sla_config: Annotated[
        Path, typer.Option("--sla-config", help="Path to the per-feed SLA YAML file")
    ] = Path("config/sla.yaml"),
    out: Annotated[
        Path, typer.Option("--out", help="Where to write the dependency DAG")
    ] = Path("docs/dependency_dag.md"),
) -> None:
    """Regenerate the dependency DAG, highlighting the blast radius of any
    feed currently breaching or approaching its SLA."""
    from pdw.db import get_connection
    from pdw.ops import compute_feed_status, load_sla, render_dependency_dag

    sla_definitions = load_sla(sla_config)
    with get_connection() as conn:
        statuses = compute_feed_status(conn, sla_definitions)

    mermaid = render_dependency_dag(statuses)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# Dependency DAG\n\n"
        "Auto-generated by `pdw ops deps` - node color reflects each feed's "
        "current SLA status; a dashed orange border marks everything downstream of a feed that "
        "is currently stale or in breach (the blast radius of that feed failing).\n\n"
        f"```mermaid\n{mermaid}```\n",
        encoding="utf-8",
    )
    typer.echo(f"wrote dependency DAG to {out}")
