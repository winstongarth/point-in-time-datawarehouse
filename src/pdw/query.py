from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import polars as pl
import psycopg
from psycopg.rows import dict_row

_FUNDAMENTALS_SCHEMA_DEF: dict[str, Any] = {
    "fact_id": pl.Int64,
    "entity_id": pl.Int64,
    "cik": pl.Utf8,
    "ticker": pl.Utf8,
    "metric_code": pl.Utf8,
    "period_start": pl.Date,
    "period_end": pl.Date,
    "fiscal_year": pl.Int64,
    "fiscal_period": pl.Utf8,
    "value": pl.Float64,
    "unit": pl.Utf8,
    "source": pl.Utf8,
    "vendor_native_tag": pl.Utf8,
    "form_type": pl.Utf8,
    "accession_no": pl.Utf8,
    "filed_date": pl.Date,
    "knowledge_from": pl.Datetime(time_zone="UTC"),
}
_FUNDAMENTALS_SCHEMA = pl.Schema(_FUNDAMENTALS_SCHEMA_DEF)

_PRICES_SCHEMA_DEF: dict[str, Any] = {
    "fact_id": pl.Int64,
    "entity_id": pl.Int64,
    "cik": pl.Utf8,
    "ticker": pl.Utf8,
    "trade_date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
    "adj_close": pl.Float64,
    "source": pl.Utf8,
    "knowledge_from": pl.Datetime(time_zone="UTC"),
}
_PRICES_SCHEMA = pl.Schema(_PRICES_SCHEMA_DEF)


class PointInTimeReader:
    """The only sanctioned way to read `core`. Every query is
    filtered to what was knowable as of a single instant - never "now"
    unless you explicitly ask for that via `.latest()`.
    """

    def __init__(self, conn: psycopg.Connection, as_of: datetime) -> None:
        if as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
            raise ValueError("as_of must be timezone-aware")
        self._conn = conn
        self.as_of = as_of

    def latest(self) -> PointInTimeReader:
        """A reader as of right now - the "restated" side of the M7 contrast experiment."""
        return PointInTimeReader(self._conn, datetime.now(UTC))

    def fundamentals(self, metrics: list[str], tickers: list[str] | None = None) -> pl.DataFrame:
        query = """
            SELECT f.fact_id, f.entity_id, e.cik, t.ticker, f.metric_code,
                   f.period_start, f.period_end, f.fiscal_year, f.fiscal_period,
                   f.value, f.unit, f.source, f.vendor_native_tag, f.form_type,
                   f.accession_no, f.filed_date, f.knowledge_from
            FROM core.fundamental_fact f
            JOIN core.entity e ON e.entity_id = f.entity_id
            JOIN core.entity_ticker t
                ON t.entity_id = e.entity_id
               AND t.knowledge_from <= %(as_of)s AND t.knowledge_to > %(as_of)s
            WHERE f.knowledge_from <= %(as_of)s AND f.knowledge_to > %(as_of)s
              AND f.metric_code = ANY(%(metrics)s)
        """
        params: dict[str, Any] = {"as_of": self.as_of, "metrics": metrics}
        if tickers is not None:
            query += " AND t.ticker = ANY(%(tickers)s)"
            params["tickers"] = tickers

        rows = self._fetch(query, params)

        # Belt-and-braces: the DB's own fundamental_fact_lag_check
        # already makes this impossible, but a reader bug (e.g. a stray `OR`)
        # must never silently leak knowledge from the future.
        for row in rows:
            if row["filed_date"] > self.as_of.date():
                raise RuntimeError(
                    f"PointInTimeReader invariant violated: fact_id={row['fact_id']} has "
                    f"filed_date {row['filed_date']} after as_of {self.as_of}"
                )

        return _to_dataframe(rows, _FUNDAMENTALS_SCHEMA)

    def prices(
        self, tickers: list[str], start: date, end: date, source: str | None = None
    ) -> pl.DataFrame:
        """`source=None` (the default) returns every vendor's row for a
        trade_date - multiple sources can hold independent, simultaneously-
        valid rows for the same entity/date, which is exactly what the
        cross-vendor reconciliation check needs. Any caller that wants
        exactly one price per ticker per day (e.g. computing a return
        series) must pass `source` explicitly - yfinance is the primary
        price source and Tiingo is secondary/reconciliation-only, so
        `source="yfinance"` is the right default for analysis code. Found
        live: querying two tickers' prices once Tiingo data existed
        alongside yfinance returned 2 rows per trade_date with no way to
        tell them apart without this parameter.
        """
        query = """
            SELECT p.fact_id, p.entity_id, e.cik, t.ticker, p.trade_date,
                   p.open, p.high, p.low, p.close, p.volume, p.adj_close,
                   p.source, p.knowledge_from
            FROM core.price_fact p
            JOIN core.entity e ON e.entity_id = p.entity_id
            JOIN core.entity_ticker t
                ON t.entity_id = e.entity_id
               AND t.knowledge_from <= %(as_of)s AND t.knowledge_to > %(as_of)s
            WHERE p.knowledge_from <= %(as_of)s AND p.knowledge_to > %(as_of)s
              AND t.ticker = ANY(%(tickers)s)
              AND p.trade_date BETWEEN %(start)s AND %(end)s
        """
        params: dict[str, Any] = {
            "as_of": self.as_of,
            "tickers": tickers,
            "start": start,
            "end": end,
        }
        if source is not None:
            query += " AND p.source = %(source)s"
            params["source"] = source
        rows = self._fetch(query, params)
        return _to_dataframe(rows, _PRICES_SCHEMA)

    def _fetch(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return cur.fetchall()


def _to_dataframe(rows: list[dict[str, Any]], schema: pl.Schema) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema=schema)
