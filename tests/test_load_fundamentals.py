from __future__ import annotations

from datetime import date

import psycopg
import pytest

from pdw.availability import SourceAvailability
from pdw.load_fundamentals import load_fundamentals

AVAILABILITY = SourceAvailability(availability_lag_days=1)


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


def _make_entity(conn: psycopg.Connection, cik: str, name: str = "Test Corp") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.entity (cik, name) VALUES (%s, %s) ON CONFLICT (cik) DO NOTHING",
            (cik, name),
        )
    conn.commit()


def _seed_stg_row(
    conn: psycopg.Connection,
    *,
    cik: str,
    metric_code: str = "revenue",
    period_end: date,
    accession_no: str,
    filed_date: date,
    value: float,
    unit: str = "USD",
    payload_id: int,
    period_start: date | None = None,
    fiscal_year: int | None = 2020,
    fiscal_period: str | None = "FY",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO stg.edgar_fundamental_fact
                (cik, entity_name, metric_code, period_start, period_end, fiscal_year,
                 fiscal_period, value, unit, vendor_native_tag, form_type, accession_no,
                 filed_date, payload_id)
            VALUES (%s, 'Test Corp', %s, %s, %s, %s, %s, %s, %s, 'SomeTag', '10-K', %s, %s, %s)
            """,
            (
                cik,
                metric_code,
                period_start,
                period_end,
                fiscal_year,
                fiscal_period,
                value,
                unit,
                accession_no,
                filed_date,
                payload_id,
            ),
        )
    conn.commit()


def _core_rows(conn: psycopg.Connection, cik: str) -> list[tuple]:
    # Both timestamps cast to ::text: psycopg3 cannot deserialize a
    # timestamptz value of 'infinity' into a Python datetime (it raises
    # DataError on read, even though writing that value works fine) -
    # reading everything as text sidesteps that, and Postgres's text
    # representation is a consistent, lexicographically-ordered format, so
    # equality/ordering assertions on the raw strings still work correctly.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.value, f.knowledge_from::text, f.knowledge_to::text,
                   f.supersedes, f.accession_no, f.fact_id
            FROM core.fundamental_fact f
            JOIN core.entity e ON e.entity_id = f.entity_id
            WHERE e.cik = %s
            ORDER BY f.knowledge_from
            """,
            (cik,),
        )
        return cur.fetchall()


def test_simple_amendment_produces_two_contiguous_non_overlapping_rows(
    db_connection: psycopg.Connection,
) -> None:
    cik = "1111111101"
    _make_entity(db_connection, cik)
    payload_id = _make_payload_id(db_connection)
    period_end = date(2020, 12, 31)

    _seed_stg_row(
        db_connection,
        cik=cik,
        period_end=period_end,
        accession_no="acc-original",
        filed_date=date(2021, 2, 1),
        value=100,
        payload_id=payload_id,
    )
    _seed_stg_row(
        db_connection,
        cik=cik,
        period_end=period_end,
        accession_no="acc-amended",
        filed_date=date(2021, 6, 1),
        value=110,
        payload_id=payload_id,
    )

    load_fundamentals(db_connection, AVAILABILITY)
    rows = _core_rows(db_connection, cik)

    assert len(rows) == 2
    original, amended = rows
    assert float(original[0]) == 100
    assert float(amended[0]) == 110
    # contiguous: the original's window ends exactly where the amendment's begins
    assert original[2] == amended[1]
    # non-overlapping: strictly increasing knowledge_from
    assert original[1] < amended[1]
    assert original[3] is None
    assert amended[3] == original[5]  # amended.supersedes == original.fact_id


def test_quarterly_and_ytd_figures_sharing_a_period_end_are_independent_facts(
    db_connection: psycopg.Connection,
) -> None:
    """Regression test for a real bug found live (2026-07-20): a Verizon
    10-Q (accession 0000732712-19-000052) reports revenue for both the
    3-month quarter and the 6-month year-to-date window ending on the same
    period_end, under the same accession. These are two different,
    simultaneously-true facts, not one restating the other - period_start
    must be part of the key, or the loader
    collides them into one and violates knowledge_from < knowledge_to.
    """
    cik = "1111111106"
    _make_entity(db_connection, cik)
    payload_id = _make_payload_id(db_connection)
    period_end = date(2018, 6, 30)

    _seed_stg_row(
        db_connection,
        cik=cik,
        period_end=period_end,
        period_start=date(2018, 4, 1),  # 3-month quarter
        accession_no="acc-verizon",
        filed_date=date(2019, 8, 8),
        value=32203000000,
        payload_id=payload_id,
    )
    _seed_stg_row(
        db_connection,
        cik=cik,
        period_end=period_end,
        period_start=date(2018, 1, 1),  # 6-month year-to-date
        accession_no="acc-verizon",
        filed_date=date(2019, 8, 8),
        value=63975000000,
        payload_id=payload_id,
    )

    load_fundamentals(db_connection, AVAILABILITY)  # must not raise
    rows = _core_rows(db_connection, cik)

    assert len(rows) == 2
    assert {float(r[0]) for r in rows} == {32203000000, 63975000000}
    # independent facts, not a restatement chain
    assert rows[0][3] is None
    assert rows[1][3] is None
    assert rows[0][2] == "infinity"
    assert rows[1][2] == "infinity"


def test_same_day_distinct_valued_accessions_are_staggered_not_rejected(
    db_connection: psycopg.Connection,
) -> None:
    """EDGAR's `filed` field is date-only. Two accessions filed the same
    calendar date with genuinely different values for the same period must
    still produce a valid, strictly-ordered chain."""
    cik = "1111111107"
    _make_entity(db_connection, cik)
    payload_id = _make_payload_id(db_connection)
    period_end = date(2020, 12, 31)
    same_day = date(2021, 2, 1)

    _seed_stg_row(
        db_connection,
        cik=cik,
        period_end=period_end,
        accession_no="acc-early",
        filed_date=same_day,
        value=100,
        payload_id=payload_id,
    )
    _seed_stg_row(
        db_connection,
        cik=cik,
        period_end=period_end,
        accession_no="acc-later-same-day",
        filed_date=same_day,
        value=105,
        payload_id=payload_id,
    )

    load_fundamentals(db_connection, AVAILABILITY)  # must not raise
    rows = _core_rows(db_connection, cik)

    assert len(rows) == 2
    assert rows[0][1] < rows[1][1]  # strictly increasing knowledge_from
    assert rows[0][2] == rows[1][1]  # contiguous


def test_double_amendment_produces_three_chained_rows(db_connection: psycopg.Connection) -> None:
    cik = "1111111102"
    _make_entity(db_connection, cik)
    payload_id = _make_payload_id(db_connection)
    period_end = date(2020, 12, 31)

    for accession, filed, value in [
        ("acc-v1", date(2021, 2, 1), 100),
        ("acc-v2", date(2021, 6, 1), 110),
        ("acc-v3", date(2021, 9, 1), 105),
    ]:
        _seed_stg_row(
            db_connection,
            cik=cik,
            period_end=period_end,
            accession_no=accession,
            filed_date=filed,
            value=value,
            payload_id=payload_id,
        )

    load_fundamentals(db_connection, AVAILABILITY)
    rows = _core_rows(db_connection, cik)

    assert len(rows) == 3
    assert [float(r[0]) for r in rows] == [100, 110, 105]
    assert rows[0][3] is None
    assert rows[1][3] == rows[0][5]
    assert rows[2][3] == rows[1][5]
    # contiguous and non-overlapping end to end
    assert rows[0][2] == rows[1][1]
    assert rows[1][2] == rows[2][1]


def test_no_change_refetch_is_a_no_op(db_connection: psycopg.Connection) -> None:
    cik = "1111111103"
    _make_entity(db_connection, cik)
    payload_id = _make_payload_id(db_connection)
    period_end = date(2020, 12, 31)

    _seed_stg_row(
        db_connection,
        cik=cik,
        period_end=period_end,
        accession_no="acc-original",
        filed_date=date(2021, 2, 1),
        value=100,
        payload_id=payload_id,
    )
    load_fundamentals(db_connection, AVAILABILITY)

    # A later filing re-reports the *same* value as a comparative figure -
    # not a restatement, must not open a new knowledge window.
    _seed_stg_row(
        db_connection,
        cik=cik,
        period_end=period_end,
        accession_no="acc-comparative-rereport",
        filed_date=date(2021, 9, 1),
        value=100,
        payload_id=payload_id,
    )
    load_fundamentals(db_connection, AVAILABILITY)

    rows = _core_rows(db_connection, cik)
    assert len(rows) == 1
    assert rows[0][4] == "acc-original"


def test_out_of_order_arrival_backfills_supersedes(db_connection: psycopg.Connection) -> None:
    cik = "1111111104"
    _make_entity(db_connection, cik)
    payload_id = _make_payload_id(db_connection)
    period_end = date(2020, 12, 31)

    # The amendment arrives and is loaded *before* we ever see the original.
    _seed_stg_row(
        db_connection,
        cik=cik,
        period_end=period_end,
        accession_no="acc-amended",
        filed_date=date(2021, 6, 1),
        value=110,
        payload_id=payload_id,
    )
    load_fundamentals(db_connection, AVAILABILITY)
    rows = _core_rows(db_connection, cik)
    assert len(rows) == 1
    assert rows[0][3] is None  # no earlier version known yet

    # The original now shows up (e.g. a backfilled ingest of older history).
    _seed_stg_row(
        db_connection,
        cik=cik,
        period_end=period_end,
        accession_no="acc-original",
        filed_date=date(2021, 2, 1),
        value=100,
        payload_id=payload_id,
    )
    load_fundamentals(db_connection, AVAILABILITY)
    rows = _core_rows(db_connection, cik)

    assert len(rows) == 2
    original, amended = rows
    assert float(original[0]) == 100
    assert float(amended[0]) == 110
    assert original[3] is None
    assert amended[3] == original[5]  # backfilled: amended now supersedes original
    assert original[2] == amended[1]  # contiguous


def test_second_run_with_no_new_data_inserts_zero_rows(db_connection: psycopg.Connection) -> None:
    cik = "1111111105"
    _make_entity(db_connection, cik)
    payload_id = _make_payload_id(db_connection)

    _seed_stg_row(
        db_connection,
        cik=cik,
        period_end=date(2020, 12, 31),
        accession_no="acc-a",
        filed_date=date(2021, 2, 1),
        value=100,
        payload_id=payload_id,
    )

    first = load_fundamentals(db_connection, AVAILABILITY)
    second = load_fundamentals(db_connection, AVAILABILITY)

    assert first.rows_inserted == 1
    assert second.rows_inserted == 0


def test_entity_without_core_row_is_skipped_not_raised(
    db_connection: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    payload_id = _make_payload_id(db_connection)
    _seed_stg_row(
        db_connection,
        cik="9999999999",  # deliberately never inserted into core.entity
        period_end=date(2020, 12, 31),
        accession_no="acc-orphan",
        filed_date=date(2021, 2, 1),
        value=100,
        payload_id=payload_id,
    )

    with caplog.at_level("WARNING"):
        summary = load_fundamentals(db_connection, AVAILABILITY)

    assert summary.rows_inserted == 0
    assert any("no core.entity" in message for message in caplog.messages)
