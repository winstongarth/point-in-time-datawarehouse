from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pdw.availability import SourceAvailability
from pdw.load_prices import load_prices, parse_tiingo_json, parse_yfinance_csv

AVAILABILITY = SourceAvailability(availability_lag_days=1)
YFINANCE_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "yfinance" / "aapl_history_sample.csv"
)


def _insert_price_payload(
    conn: psycopg.Connection, *, ticker: str, body: bytes, fetched_at: datetime
) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ops.pipeline_run (pipeline) VALUES ('test') RETURNING run_id")
        row = cur.fetchone()
        assert row is not None
        run_id = row[0]
        cur.execute(
            """
            INSERT INTO raw.payload
                (source, endpoint, request_params, fetched_at, http_status,
                 content_sha256, body, run_id)
            VALUES ('yfinance', 'history', %s, %s, 200, %s, %s, %s)
            """,
            (
                Jsonb({"ticker": ticker}),
                fetched_at,
                hashlib.sha256(body).hexdigest(),
                body,
                run_id,
            ),
        )
    conn.commit()


def _seed_entity_and_ticker(conn: psycopg.Connection, cik: str, ticker: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.entity (cik, name) VALUES (%s, 'Test Corp') "
            "ON CONFLICT (cik) DO NOTHING",
            (cik,),
        )
        cur.execute(
            "INSERT INTO core.entity_ticker (entity_id, ticker, knowledge_from) "
            "SELECT entity_id, %s, now() FROM core.entity WHERE cik = %s",
            (ticker, cik),
        )
    conn.commit()


def _price_rows(conn: psycopg.Connection, cik: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.trade_date, f.close, f.adj_close, f.knowledge_from::text,
                   f.knowledge_to::text
            FROM core.price_fact f
            JOIN core.entity e ON e.entity_id = f.entity_id
            WHERE e.cik = %s
            ORDER BY f.trade_date, f.knowledge_from
            """,
            (cik,),
        )
        return cur.fetchall()


def _csv_body(rows: list[str]) -> bytes:
    header = "Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits"
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


def test_parse_yfinance_csv_matches_real_fixture() -> None:
    rows = parse_yfinance_csv(YFINANCE_FIXTURE.read_bytes())

    assert len(rows) == 6
    assert rows[0].trade_date == date(2026, 7, 10)
    assert rows[-1].trade_date == date(2026, 7, 17)
    assert all(r.close is not None and r.adj_close is not None for r in rows)


def test_parse_tiingo_json() -> None:
    body = (
        b'[{"date": "2026-07-17T00:00:00.000Z", "close": 333.74, "high": 334.99, '
        b'"low": 329.0, "open": 331.98, "volume": 63365300, "adjClose": 333.74, '
        b'"adjHigh": 334.99, "adjLow": 329.0, "adjOpen": 331.98, "adjVolume": 63365300, '
        b'"divCash": 0.0, "splitFactor": 1.0}]'
    )

    rows = parse_tiingo_json(body)

    assert len(rows) == 1
    assert rows[0].trade_date == date(2026, 7, 17)
    assert rows[0].close == 333.74


def test_first_fetch_backdates_knowledge_from_to_trade_date(
    db_connection: psycopg.Connection,
) -> None:
    cik, ticker = "3333333301", "TEST1"
    _seed_entity_and_ticker(db_connection, cik, ticker)

    body = _csv_body(["2024-01-02 00:00:00-05:00,10,11,9,10.5,10.5,1000,0.0,0.0"])
    _insert_price_payload(
        db_connection, ticker=ticker, body=body, fetched_at=datetime(2026, 1, 1, tzinfo=UTC)
    )

    load_prices(db_connection, "yfinance", AVAILABILITY)
    rows = _price_rows(db_connection, cik)

    assert len(rows) == 1
    trade_date, close, adj_close, knowledge_from, knowledge_to = rows[0]
    assert trade_date == date(2024, 1, 2)
    assert float(close) == 10.5
    # backdated to (trade_date + lag), not the 2026 fetch time
    assert knowledge_from.startswith("2024-01-03")
    assert knowledge_to == "infinity"


def test_retroactive_adjustment_opens_a_new_window_at_detection_time(
    db_connection: psycopg.Connection,
) -> None:
    cik, ticker = "3333333302", "TEST2"
    _seed_entity_and_ticker(db_connection, cik, ticker)

    first_fetch = datetime(2026, 1, 1, tzinfo=UTC)
    second_fetch = datetime(2026, 6, 1, tzinfo=UTC)

    _insert_price_payload(
        db_connection,
        ticker=ticker,
        body=_csv_body(["2024-01-02 00:00:00-05:00,10,11,9,10.5,10.5,1000,0.0,0.0"]),
        fetched_at=first_fetch,
    )
    # A later fetch reveals the adjusted close for the same trade_date has
    # silently changed (e.g. a stock split applied retroactively).
    _insert_price_payload(
        db_connection,
        ticker=ticker,
        body=_csv_body(["2024-01-02 00:00:00-05:00,10,11,9,10.5,5.25,2000,0.0,0.0"]),
        fetched_at=second_fetch,
    )

    load_prices(db_connection, "yfinance", AVAILABILITY)
    rows = _price_rows(db_connection, cik)

    assert len(rows) == 2
    original, adjusted = rows
    assert float(original[2]) == 10.5  # original adj_close
    assert float(adjusted[2]) == 5.25  # adjusted adj_close
    assert original[3].startswith("2024-01-03")  # backdated
    assert adjusted[3].startswith("2026-06-01")  # knowledge_from = detection fetch time
    assert original[4] == adjusted[3]  # contiguous
    assert adjusted[4] == "infinity"


def test_identical_refetch_is_a_no_op(db_connection: psycopg.Connection) -> None:
    cik, ticker = "3333333303", "TEST3"
    _seed_entity_and_ticker(db_connection, cik, ticker)

    body = _csv_body(["2024-01-02 00:00:00-05:00,10,11,9,10.5,10.5,1000,0.0,0.0"])
    _insert_price_payload(
        db_connection, ticker=ticker, body=body, fetched_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    _insert_price_payload(
        db_connection, ticker=ticker, body=body, fetched_at=datetime(2026, 6, 1, tzinfo=UTC)
    )

    load_prices(db_connection, "yfinance", AVAILABILITY)
    rows = _price_rows(db_connection, cik)

    assert len(rows) == 1


def test_second_run_with_no_new_payloads_inserts_zero_rows(
    db_connection: psycopg.Connection,
) -> None:
    cik, ticker = "3333333304", "TEST4"
    _seed_entity_and_ticker(db_connection, cik, ticker)

    body = _csv_body(["2024-01-02 00:00:00-05:00,10,11,9,10.5,10.5,1000,0.0,0.0"])
    _insert_price_payload(
        db_connection, ticker=ticker, body=body, fetched_at=datetime(2026, 1, 1, tzinfo=UTC)
    )

    first = load_prices(db_connection, "yfinance", AVAILABILITY)
    second = load_prices(db_connection, "yfinance", AVAILABILITY)

    assert first.rows_inserted == 1
    assert second.rows_inserted == 0


def test_ticker_without_entity_mapping_is_skipped_not_raised(
    db_connection: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    body = _csv_body(["2024-01-02 00:00:00-05:00,10,11,9,10.5,10.5,1000,0.0,0.0"])
    _insert_price_payload(
        db_connection,
        ticker="NOMAPPING",
        body=body,
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    with caplog.at_level("WARNING"):
        summary = load_prices(db_connection, "yfinance", AVAILABILITY)

    assert summary.rows_inserted == 0
    assert any("no core.entity_ticker" in message for message in caplog.messages)


def test_unknown_source_raises() -> None:
    with pytest.raises(ValueError, match="unknown price source"):
        load_prices(None, "not-a-real-source", AVAILABILITY)  # type: ignore[arg-type]
