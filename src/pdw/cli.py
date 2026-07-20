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
