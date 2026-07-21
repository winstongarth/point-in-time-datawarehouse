from __future__ import annotations

import logging
from pathlib import Path

import psycopg
import yaml
from psycopg.types.json import Jsonb

from pdw.db import pipeline_run
from pdw.sources import FetchResult, Source

logger = logging.getLogger(__name__)


def build_source(name: str) -> Source:
    """Construct the adapter for `name`, importing it lazily.

    Each adapter module pulls in its own (sometimes heavy, e.g. yfinance's
    pandas/curl_cffi chain) dependencies, so only the one actually requested
    gets imported.
    """
    if name == "edgar":
        from pdw.sources.edgar import EdgarSource

        return EdgarSource()
    if name == "yfinance":
        from pdw.sources.yfinance_source import YFinanceSource

        return YFinanceSource()
    if name == "tiingo":
        from pdw.sources.tiingo import TiingoSource

        return TiingoSource()
    raise ValueError(f"unknown source {name!r}, expected one of: edgar, yfinance, tiingo")


def load_universe(path: Path) -> list[str]:
    data = yaml.safe_load(path.read_text())
    tickers = data.get("tickers") if isinstance(data, dict) else None
    if not tickers:
        raise ValueError(f"universe file {path} has no 'tickers' list")
    return [str(ticker) for ticker in tickers]


def ingest(conn: psycopg.Connection, source: Source, tickers: list[str]) -> None:
    """Fetch every vendor response for `tickers` via `source` and land it in raw.payload.

    Runs inside one ops.pipeline_run row so the
    whole ingest is traceable to a single run_id, win or lose.
    """
    with pipeline_run(conn, pipeline=f"ingest:{source.name}") as run:
        for result in source.fetch_universe(tickers):
            run.rows_in += 1
            previously_seen = _hash_already_seen(conn, source.name, result.content_sha256)
            _write_payload(conn, source.name, result, run.run_id)
            conn.commit()
            run.rows_out += 1
            logger.info(
                "wrote raw.payload row",
                extra={
                    "source": source.name,
                    "endpoint": result.endpoint,
                    "request_params": result.request_params,
                    "http_status": result.http_status,
                    "content_sha256": result.content_sha256,
                    "content_unchanged": previously_seen,
                },
            )


def _hash_already_seen(conn: psycopg.Connection, source: str, content_sha256: str) -> bool:
    """Whether this exact body has already landed for this source in a prior fetch.

    Purely observational (logged, not acted on) — raw.payload always records
    every fetch regardless: proof of no-change is itself information.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM raw.payload WHERE source = %s AND content_sha256 = %s)",
            (source, content_sha256),
        )
        row = cur.fetchone()
        assert row is not None
        return bool(row[0])


def _write_payload(
    conn: psycopg.Connection, source: str, result: FetchResult, run_id: int
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.payload
                (source, endpoint, request_params, fetched_at, http_status,
                 content_sha256, body, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                source,
                result.endpoint,
                Jsonb(result.request_params),
                result.fetched_at,
                result.http_status,
                result.content_sha256,
                result.body,
                run_id,
            ),
        )
