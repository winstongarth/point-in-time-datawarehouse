from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg

from pdw.availability import SourceAvailability, compute_knowledge_from

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceRow:
    trade_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    adj_close: float | None


def parse_yfinance_csv(body: bytes) -> list[PriceRow]:
    """Parse the CSV this project's YFinanceSource writes as its raw.payload
    body (the Close/Adj Close divergence lives in this shape)."""
    reader = csv.DictReader(io.StringIO(body.decode("utf-8")))
    rows = []
    for record in reader:
        trade_date = datetime.fromisoformat(record["Date"]).date()
        rows.append(
            PriceRow(
                trade_date=trade_date,
                open=_to_float(record["Open"]),
                high=_to_float(record["High"]),
                low=_to_float(record["Low"]),
                close=_to_float(record["Close"]),
                volume=_to_int(record["Volume"]),
                adj_close=_to_float(record["Adj Close"]),
            )
        )
    return rows


def parse_tiingo_json(body: bytes) -> list[PriceRow]:
    """Parse Tiingo's EOD prices JSON shape."""
    data = json.loads(body)
    rows = []
    for point in data:
        trade_date = datetime.fromisoformat(point["date"].replace("Z", "+00:00")).date()
        rows.append(
            PriceRow(
                trade_date=trade_date,
                open=point.get("open"),
                high=point.get("high"),
                low=point.get("low"),
                close=point.get("close"),
                volume=point.get("volume"),
                adj_close=point.get("adjClose"),
            )
        )
    return rows


def _to_float(text: str) -> float | None:
    return None if text == "" else float(text)


def _to_int(text: str) -> int | None:
    return None if text == "" else int(float(text))


_PARSERS = {
    "yfinance": parse_yfinance_csv,
    "tiingo": parse_tiingo_json,
}


@dataclass
class LoadSummary:
    keys_processed: int = 0
    rows_inserted: int = 0
    rows_relinked: int = 0


def load_prices(
    conn: psycopg.Connection, source: str, availability: SourceAvailability
) -> LoadSummary:
    """Promote raw.payload price history for `source` into core.price_fact.

    Unlike fundamentals, a single price fetch doesn't carry its own
    point-in-time history - it's a snapshot of *today's* view of history, so
    detecting a retroactive adjustment (a vendor's adjusted-close history
    for past dates silently changing) requires
    comparing *across* separate fetches taken at different real times, not
    within one payload. A trade_date's first-ever observed value is
    backdated to when it was actually knowable (trade_date + availability
    lag); a later fetch reporting a *different* value for that same
    trade_date is a real correction, timestamped at the fetch that
    discovered it - we didn't and couldn't know about it any earlier.
    """
    parser = _PARSERS.get(source)
    if parser is None:
        raise ValueError(f"unknown price source {source!r}, expected one of: {list(_PARSERS)}")

    summary = LoadSummary()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT request_params->>'ticker' FROM raw.payload "
            "WHERE source = %s AND endpoint IN ('history', 'prices')",
            (source,),
        )
        tickers = [row[0] for row in cur.fetchall() if row[0] is not None]

    for ticker in tickers:
        cik = _get_cik_for_ticker(conn, ticker)
        if cik is None:
            logger.warning(
                "no core.entity_ticker for ticker, skipping price load",
                extra={"ticker": ticker, "source": source},
            )
            continue
        entity_id = _get_entity_id(conn, cik)
        if entity_id is None:
            continue

        payloads = _fetch_payloads(conn, source, ticker)
        chain_by_date = _build_chains(payloads, parser, availability.availability_lag_days)
        for trade_date, chain in chain_by_date.items():
            _reconcile(conn, entity_id, source, trade_date, chain, summary)
        summary.keys_processed += 1

    conn.commit()
    return summary


def _get_cik_for_ticker(conn: psycopg.Connection, ticker: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT e.cik FROM core.entity_ticker t "
            "JOIN core.entity e ON e.entity_id = t.entity_id "
            "WHERE t.ticker = %s AND t.knowledge_to = 'infinity'",
            (ticker,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _get_entity_id(conn: psycopg.Connection, cik: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SELECT entity_id FROM core.entity WHERE cik = %s", (cik,))
        row = cur.fetchone()
        return row[0] if row else None


def _fetch_payloads(
    conn: psycopg.Connection, source: str, ticker: str
) -> list[tuple[int, datetime, bytes]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload_id, fetched_at, body FROM raw.payload
            WHERE source = %s AND endpoint IN ('history', 'prices')
                  AND request_params->>'ticker' = %s AND http_status = 200
            ORDER BY fetched_at ASC
            """,
            (source, ticker),
        )
        return [(row[0], row[1], bytes(row[2])) for row in cur.fetchall()]


@dataclass(frozen=True)
class PriceVersion:
    close: float | None
    adj_close: float | None
    open: float | None
    high: float | None
    low: float | None
    volume: int | None
    payload_id: int
    knowledge_from: datetime


def _build_chains(
    payloads: list[tuple[int, datetime, bytes]], parser: Any, lag_days: int
) -> dict[date, list[PriceVersion]]:
    chains: dict[date, list[PriceVersion]] = {}
    current: dict[date, tuple[float | None, float | None]] = {}

    for payload_id, fetched_at, body in payloads:
        for row in parser(body):
            key = (row.close, row.adj_close)
            if row.trade_date not in current:
                # First time this trade_date has ever been observed: it was
                # knowable historically, at trade_date + lag, regardless of
                # when we happened to fetch it.
                current[row.trade_date] = key
                chains[row.trade_date] = [
                    PriceVersion(
                        close=row.close,
                        adj_close=row.adj_close,
                        open=row.open,
                        high=row.high,
                        low=row.low,
                        volume=row.volume,
                        payload_id=payload_id,
                        knowledge_from=compute_knowledge_from(row.trade_date, lag_days),
                    )
                ]
            elif current[row.trade_date] != key:
                # A later fetch disagrees with what we previously saw for
                # this trade_date: a genuine retroactive adjustment,
                # knowable only from the moment we detected it.
                current[row.trade_date] = key
                chains[row.trade_date].append(
                    PriceVersion(
                        close=row.close,
                        adj_close=row.adj_close,
                        open=row.open,
                        high=row.high,
                        low=row.low,
                        volume=row.volume,
                        payload_id=payload_id,
                        knowledge_from=fetched_at,
                    )
                )
            # else: identical to what we already have - a no-op re-fetch.

    return chains


def _reconcile(
    conn: psycopg.Connection,
    entity_id: int,
    source: str,
    trade_date: date,
    chain: list[PriceVersion],
    summary: LoadSummary,
) -> None:
    # No supersedes tracking here (unlike fundamentals): price_fact has no
    # such column - each version stands on its own, linked
    # only by (entity_id, trade_date, source) and its knowledge window.
    existing = _existing_rows(conn, entity_id, source, trade_date)

    for i, version in enumerate(chain):
        knowledge_to: object = chain[i + 1].knowledge_from if i + 1 < len(chain) else "infinity"
        fact_id = existing.get(version.knowledge_from)

        if fact_id is None:
            _insert_price(conn, entity_id, source, trade_date, version, knowledge_to)
            summary.rows_inserted += 1
        else:
            _relink_price(conn, fact_id, knowledge_to)
            summary.rows_relinked += 1


def _existing_rows(
    conn: psycopg.Connection, entity_id: int, source: str, trade_date: date
) -> dict[datetime, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT knowledge_from, fact_id FROM core.price_fact
            WHERE entity_id = %s AND source = %s AND trade_date = %s
            """,
            (entity_id, source, trade_date),
        )
        return dict(cur.fetchall())


def _insert_price(
    conn: psycopg.Connection,
    entity_id: int,
    source: str,
    trade_date: date,
    version: PriceVersion,
    knowledge_to: object,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.price_fact
                (entity_id, trade_date, open, high, low, close, volume, adj_close,
                 source, knowledge_from, knowledge_to, payload_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s)
            RETURNING fact_id
            """,
            (
                entity_id,
                trade_date,
                version.open,
                version.high,
                version.low,
                version.close,
                version.volume,
                version.adj_close,
                source,
                version.knowledge_from,
                knowledge_to,
                version.payload_id,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        fact_id: int = row[0]
        return fact_id


def _relink_price(conn: psycopg.Connection, fact_id: int, knowledge_to: object) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.price_fact SET knowledge_to = %s::timestamptz WHERE fact_id = %s",
            (knowledge_to, fact_id),
        )
