from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from pdw.query import PointInTimeReader


def _make_payload_id(conn: psycopg.Connection) -> int:
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
            VALUES ('edgar', 'companyfacts', '{}'::jsonb, now(), 200, repeat('0', 64), 'x', %s)
            RETURNING payload_id
            """,
            (run_id,),
        )
        row = cur.fetchone()
        assert row is not None
        payload_id: int = row[0]
    conn.commit()
    return payload_id


def _make_entity_with_ticker(
    conn: psycopg.Connection,
    cik: str,
    ticker: str,
    ticker_knowledge_from: datetime | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.entity (cik, name) VALUES (%s, 'Test Corp') RETURNING entity_id",
            (cik,),
        )
        row = cur.fetchone()
        assert row is not None
        entity_id: int = row[0]
        cur.execute(
            "INSERT INTO core.entity_ticker (entity_id, ticker, knowledge_from) "
            "VALUES (%s, %s, %s)",
            (entity_id, ticker, ticker_knowledge_from or datetime(2000, 1, 1, tzinfo=UTC)),
        )
    conn.commit()
    return entity_id


def _insert_fundamental_fact(
    conn: psycopg.Connection,
    *,
    entity_id: int,
    payload_id: int,
    metric_code: str = "revenue",
    period_end: date = date(2020, 12, 31),
    filed_date: date,
    knowledge_from: datetime,
    knowledge_to: str = "infinity",
    value: float,
    accession_no: str = "acc-1",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.fundamental_fact
                (entity_id, metric_code, period_end, value, unit, source,
                 accession_no, filed_date, knowledge_from, knowledge_to, payload_id)
            VALUES (%s, %s, %s, %s, 'USD', 'edgar', %s, %s, %s, %s::timestamptz, %s)
            """,
            (
                entity_id,
                metric_code,
                period_end,
                value,
                accession_no,
                filed_date,
                knowledge_from,
                knowledge_to,
                payload_id,
            ),
        )
    conn.commit()


def _insert_price_fact(
    conn: psycopg.Connection,
    *,
    entity_id: int,
    payload_id: int,
    trade_date: date,
    knowledge_from: datetime,
    knowledge_to: str = "infinity",
    close: float,
    adj_close: float,
    source: str = "yfinance",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.price_fact
                (entity_id, trade_date, close, adj_close, source,
                 knowledge_from, knowledge_to, payload_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s::timestamptz, %s)
            """,
            (
                entity_id,
                trade_date,
                close,
                adj_close,
                source,
                knowledge_from,
                knowledge_to,
                payload_id,
            ),
        )
    conn.commit()


def test_naive_as_of_is_rejected(db_connection: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        PointInTimeReader(db_connection, datetime(2021, 1, 1))


def test_as_of_before_and_after_a_restatement(db_connection: psycopg.Connection) -> None:
    cik, ticker = "4444444401", "REST1"
    entity_id = _make_entity_with_ticker(db_connection, cik, ticker)
    payload_id = _make_payload_id(db_connection)
    period_end = date(2020, 12, 31)

    original_kf = datetime(2021, 2, 2, 13, 30, tzinfo=UTC)
    amended_kf = datetime(2021, 6, 2, 13, 30, tzinfo=UTC)

    _insert_fundamental_fact(
        db_connection,
        entity_id=entity_id,
        payload_id=payload_id,
        period_end=period_end,
        filed_date=date(2021, 2, 1),
        knowledge_from=original_kf,
        knowledge_to=str(amended_kf),
        value=100,
        accession_no="acc-original",
    )
    _insert_fundamental_fact(
        db_connection,
        entity_id=entity_id,
        payload_id=payload_id,
        period_end=period_end,
        filed_date=date(2021, 6, 1),
        knowledge_from=amended_kf,
        value=110,
        accession_no="acc-amended",
    )

    before = PointInTimeReader(db_connection, original_kf + timedelta(days=1))
    after = PointInTimeReader(db_connection, amended_kf + timedelta(days=1))

    before_df = before.fundamentals(["revenue"], [ticker])
    after_df = after.fundamentals(["revenue"], [ticker])

    assert before_df["value"].to_list() == [100.0]
    assert after_df["value"].to_list() == [110.0]


def test_as_of_exactly_at_knowledge_from_sees_the_new_value(
    db_connection: psycopg.Connection,
) -> None:
    """knowledge_from <= as_of (inclusive) per CLAUDE.md 6's predicate."""
    cik, ticker = "4444444402", "REST2"
    entity_id = _make_entity_with_ticker(db_connection, cik, ticker)
    payload_id = _make_payload_id(db_connection)
    knowledge_from = datetime(2021, 6, 2, 13, 30, tzinfo=UTC)

    _insert_fundamental_fact(
        db_connection,
        entity_id=entity_id,
        payload_id=payload_id,
        filed_date=date(2021, 6, 1),
        knowledge_from=knowledge_from,
        value=110,
    )

    reader = PointInTimeReader(db_connection, knowledge_from)
    df = reader.fundamentals(["revenue"], [ticker])

    assert df["value"].to_list() == [110.0]


def test_never_returns_a_row_with_filed_date_after_as_of(
    db_connection: psycopg.Connection,
) -> None:
    """Property test (CLAUDE.md 6): across randomized as_of samples, no
    returned row's filed_date is ever after as_of."""
    cik, ticker = "4444444403", "PROP1"
    entity_id = _make_entity_with_ticker(db_connection, cik, ticker)
    payload_id = _make_payload_id(db_connection)

    rng = random.Random(20260720)
    for i in range(10):
        filed = date(2015, 1, 1) + timedelta(days=i * 200)
        _insert_fundamental_fact(
            db_connection,
            entity_id=entity_id,
            payload_id=payload_id,
            period_end=date(2015, 1, 1) + timedelta(days=i * 90),
            filed_date=filed,
            knowledge_from=datetime.combine(filed, datetime.min.time(), tzinfo=UTC)
            + timedelta(days=1),
            value=float(i),
            accession_no=f"acc-{i}",
        )

    for _ in range(50):
        random_as_of = datetime(2010, 1, 1, tzinfo=UTC) + timedelta(
            days=rng.randint(0, 365 * 20)
        )
        reader = PointInTimeReader(db_connection, random_as_of)
        df = reader.fundamentals(["revenue"], [ticker])
        for filed_date in df["filed_date"].to_list():
            assert filed_date <= random_as_of.date()


def test_prices_reflects_retroactive_adjustment(db_connection: psycopg.Connection) -> None:
    cik, ticker = "4444444404", "PRICE1"
    entity_id = _make_entity_with_ticker(db_connection, cik, ticker)
    payload_id = _make_payload_id(db_connection)
    trade_date = date(2024, 1, 2)

    original_kf = datetime(2024, 1, 3, 13, 30, tzinfo=UTC)
    adjusted_kf = datetime(2024, 6, 1, tzinfo=UTC)

    _insert_price_fact(
        db_connection,
        entity_id=entity_id,
        payload_id=payload_id,
        trade_date=trade_date,
        knowledge_from=original_kf,
        knowledge_to=str(adjusted_kf),
        close=10.5,
        adj_close=10.5,
    )
    _insert_price_fact(
        db_connection,
        entity_id=entity_id,
        payload_id=payload_id,
        trade_date=trade_date,
        knowledge_from=adjusted_kf,
        close=10.5,
        adj_close=5.25,
    )

    before = PointInTimeReader(db_connection, original_kf + timedelta(days=1))
    after = PointInTimeReader(db_connection, adjusted_kf + timedelta(days=1))

    before_df = before.prices([ticker], trade_date, trade_date)
    after_df = after.prices([ticker], trade_date, trade_date)

    assert before_df["adj_close"].to_list() == [10.5]
    assert after_df["adj_close"].to_list() == [5.25]


def test_prices_returns_every_source_by_default_but_filters_when_asked(
    db_connection: psycopg.Connection,
) -> None:
    """Regression: found live at M7 once Tiingo data existed alongside
    yfinance for the same tickers - prices() had no source filter, so any
    caller doing return-series arithmetic silently got 2 ambiguous rows per
    trade_date. source=None must still return both (needed by the
    cross-vendor check); source="yfinance" must return exactly one.
    """
    cik, ticker = "4444444408", "MULTISRC"
    entity_id = _make_entity_with_ticker(db_connection, cik, ticker)
    payload_id = _make_payload_id(db_connection)
    trade_date = date(2024, 1, 2)
    knowledge_from = datetime(2024, 1, 3, 13, 30, tzinfo=UTC)

    _insert_price_fact(
        db_connection, entity_id=entity_id, payload_id=payload_id, trade_date=trade_date,
        knowledge_from=knowledge_from, close=100.0, adj_close=100.0, source="yfinance",
    )
    _insert_price_fact(
        db_connection, entity_id=entity_id, payload_id=payload_id, trade_date=trade_date,
        knowledge_from=knowledge_from, close=100.5, adj_close=100.5, source="tiingo",
    )

    reader = PointInTimeReader(db_connection, knowledge_from + timedelta(days=1))

    both = reader.prices([ticker], trade_date, trade_date)
    assert sorted(both["source"].to_list()) == ["tiingo", "yfinance"]

    yf_only = reader.prices([ticker], trade_date, trade_date, source="yfinance")
    assert yf_only["source"].to_list() == ["yfinance"]
    assert yf_only["adj_close"].to_list() == [100.0]


def test_ticker_resolution_respects_its_own_knowledge_window(
    db_connection: psycopg.Connection,
) -> None:
    """entity_ticker is itself bitemporal (CLAUDE.md 5) - the reader must
    not resolve a ticker to an entity before that mapping was known."""
    cik, ticker = "4444444405", "LATETICKER"
    ticker_known_from = datetime(2022, 1, 1, tzinfo=UTC)
    entity_id = _make_entity_with_ticker(db_connection, cik, ticker, ticker_known_from)
    payload_id = _make_payload_id(db_connection)

    _insert_fundamental_fact(
        db_connection,
        entity_id=entity_id,
        payload_id=payload_id,
        filed_date=date(2021, 1, 1),
        knowledge_from=datetime(2021, 1, 2, tzinfo=UTC),
        value=100,
    )

    too_early = PointInTimeReader(db_connection, datetime(2021, 6, 1, tzinfo=UTC))
    df = too_early.fundamentals(["revenue"], [ticker])

    assert df.height == 0


def test_tickers_none_returns_all_matching_entities(db_connection: psycopg.Connection) -> None:
    cik_a, ticker_a = "4444444406", "ALLA"
    cik_b, ticker_b = "4444444407", "ALLB"
    entity_a = _make_entity_with_ticker(db_connection, cik_a, ticker_a)
    entity_b = _make_entity_with_ticker(db_connection, cik_b, ticker_b)
    payload_id = _make_payload_id(db_connection)
    as_of = datetime(2021, 6, 1, tzinfo=UTC)

    for entity_id, accession in [(entity_a, "acc-a"), (entity_b, "acc-b")]:
        _insert_fundamental_fact(
            db_connection,
            entity_id=entity_id,
            payload_id=payload_id,
            filed_date=date(2021, 1, 1),
            knowledge_from=datetime(2021, 1, 2, tzinfo=UTC),
            value=1,
            accession_no=accession,
        )

    reader = PointInTimeReader(db_connection, as_of)
    df = reader.fundamentals(["revenue"])

    assert set(df.filter(df["ticker"].is_in([ticker_a, ticker_b]))["ticker"].to_list()) == {
        ticker_a,
        ticker_b,
    }


def test_latest_uses_current_time(db_connection: psycopg.Connection) -> None:
    reader = PointInTimeReader(db_connection, datetime(2020, 1, 1, tzinfo=UTC))
    latest = reader.latest()

    assert (datetime.now(UTC) - latest.as_of) < timedelta(seconds=10)
