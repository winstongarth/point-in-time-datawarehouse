"""CLAUDE.md 5's six non-negotiable invariants, tested against the live
database - not just asserted in application code.

1. No knowledge-time overlap (the EXCLUDE constraint).
2. Exactly one open row per key (a corollary of #1: two open-to-infinity
   rows for the same key necessarily overlap).
3. knowledge_from >= filed_date/trade_date (the CHECK constraint's part of
   the story; the exact +availability_lag amount is an application-level
   concern, see pdw.availability).
4. knowledge_from < knowledge_to.
5. Full lineage: payload_id is NOT NULL and FK-enforced.
6. raw.payload is append-only - covered by tests/test_ingest.py, not
   repeated here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import psycopg
import pytest


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


def _make_entity(conn: psycopg.Connection, cik: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.entity (cik, name) VALUES (%s, 'Test Corp') RETURNING entity_id",
            (cik,),
        )
        row = cur.fetchone()
        assert row is not None
        entity_id: int = row[0]
    conn.commit()
    return entity_id


def _insert_fundamental_fact(
    conn: psycopg.Connection,
    *,
    entity_id: int,
    payload_id: int,
    period_end: date = date(2020, 12, 31),
    filed_date: date = date(2021, 2, 1),
    knowledge_from: datetime,
    knowledge_to: str = "infinity",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.fundamental_fact
                (entity_id, metric_code, period_end, value, unit, source,
                 filed_date, knowledge_from, knowledge_to, payload_id)
            VALUES (%s, 'revenue', %s, 100, 'USD', 'edgar', %s, %s, %s::timestamptz, %s)
            """,
            (entity_id, period_end, filed_date, knowledge_from, knowledge_to, payload_id),
        )


# --- Invariant 1 + 2: no overlap / exactly one open row ---------------------


def test_invariant1_rejects_two_open_rows_for_the_same_key(
    db_connection: psycopg.Connection,
) -> None:
    entity_id = _make_entity(db_connection, "2222222201")
    payload_id = _make_payload_id(db_connection)

    _insert_fundamental_fact(
        db_connection,
        entity_id=entity_id,
        payload_id=payload_id,
        knowledge_from=datetime(2021, 2, 2, 13, 30, tzinfo=UTC),
    )
    db_connection.commit()

    with pytest.raises(psycopg.errors.ExclusionViolation):
        _insert_fundamental_fact(
            db_connection,
            entity_id=entity_id,
            payload_id=payload_id,
            knowledge_from=datetime(2021, 6, 2, 13, 30, tzinfo=UTC),
        )
    db_connection.rollback()


def test_invariant1_rejects_overlapping_non_infinite_ranges(
    db_connection: psycopg.Connection,
) -> None:
    entity_id = _make_entity(db_connection, "2222222202")
    payload_id = _make_payload_id(db_connection)

    with db_connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.fundamental_fact
                (entity_id, metric_code, period_end, value, unit, source,
                 filed_date, knowledge_from, knowledge_to, payload_id)
            VALUES (%s, 'revenue', %s, 100, 'USD', 'edgar', %s, %s, %s, %s)
            """,
            (
                entity_id,
                date(2020, 12, 31),
                date(2021, 2, 1),
                datetime(2021, 2, 2, 13, 30, tzinfo=UTC),
                datetime(2021, 9, 1, 13, 30, tzinfo=UTC),
                payload_id,
            ),
        )
    db_connection.commit()

    # Overlaps the first row's [Feb 2, Sep 1) window.
    with pytest.raises(psycopg.errors.ExclusionViolation):
        _insert_fundamental_fact(
            db_connection,
            entity_id=entity_id,
            payload_id=payload_id,
            knowledge_from=datetime(2021, 6, 1, 13, 30, tzinfo=UTC),
        )
    db_connection.rollback()


def test_invariant1_allows_non_overlapping_contiguous_ranges(
    db_connection: psycopg.Connection,
) -> None:
    entity_id = _make_entity(db_connection, "2222222203")
    payload_id = _make_payload_id(db_connection)

    with db_connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.fundamental_fact
                (entity_id, metric_code, period_end, value, unit, source,
                 filed_date, knowledge_from, knowledge_to, payload_id)
            VALUES (%s, 'revenue', %s, 100, 'USD', 'edgar', %s, %s, %s, %s)
            """,
            (
                entity_id,
                date(2020, 12, 31),
                date(2021, 2, 1),
                datetime(2021, 2, 2, 13, 30, tzinfo=UTC),
                datetime(2021, 6, 1, 13, 30, tzinfo=UTC),
                payload_id,
            ),
        )
    _insert_fundamental_fact(
        db_connection,
        entity_id=entity_id,
        payload_id=payload_id,
        knowledge_from=datetime(2021, 6, 1, 13, 30, tzinfo=UTC),
    )
    db_connection.commit()  # must not raise

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM core.fundamental_fact WHERE entity_id = %s", (entity_id,)
        )
        (count,) = cur.fetchone()
    assert count == 2


def test_invariant2_at_most_one_open_row_holds_after_loading(
    db_connection: psycopg.Connection,
) -> None:
    from pdw.availability import SourceAvailability
    from pdw.load_fundamentals import load_fundamentals

    entity_id = _make_entity(db_connection, "2222222204")
    payload_id = _make_payload_id(db_connection)
    cik = "2222222204"

    with db_connection.cursor() as cur:
        for accession, filed, value in [
            ("acc-a", date(2021, 2, 1), 100),
            ("acc-b", date(2021, 6, 1), 110),
        ]:
            cur.execute(
                """
                INSERT INTO stg.edgar_fundamental_fact
                    (cik, entity_name, metric_code, period_end, fiscal_year,
                     fiscal_period, value, unit, vendor_native_tag, form_type,
                     accession_no, filed_date, payload_id)
                VALUES (%s, 'Test Corp', 'revenue', %s, 2020, 'FY', %s, 'USD',
                        'Revenues', '10-K', %s, %s, %s)
                """,
                (cik, date(2020, 12, 31), value, accession, filed, payload_id),
            )
    db_connection.commit()

    load_fundamentals(db_connection, SourceAvailability(availability_lag_days=1))

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM core.fundamental_fact "
            "WHERE entity_id = %s AND knowledge_to = 'infinity'",
            (entity_id,),
        )
        (open_count,) = cur.fetchone()
    assert open_count == 1


# --- Invariant 3: knowledge_from >= filed_date ------------------------------


def test_invariant3_rejects_knowledge_from_before_filed_date(
    db_connection: psycopg.Connection,
) -> None:
    entity_id = _make_entity(db_connection, "2222222205")
    payload_id = _make_payload_id(db_connection)

    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_fundamental_fact(
            db_connection,
            entity_id=entity_id,
            payload_id=payload_id,
            filed_date=date(2021, 6, 1),
            knowledge_from=datetime(2021, 2, 1, 13, 30, tzinfo=UTC),  # before filed_date
        )
    db_connection.rollback()


# --- Invariant 4: knowledge_from < knowledge_to -----------------------------


def test_invariant4_rejects_knowledge_from_at_or_after_knowledge_to(
    db_connection: psycopg.Connection,
) -> None:
    entity_id = _make_entity(db_connection, "2222222206")
    payload_id = _make_payload_id(db_connection)

    with pytest.raises(psycopg.errors.CheckViolation), db_connection.cursor() as cur:
        cur.execute(
            """
                INSERT INTO core.fundamental_fact
                    (entity_id, metric_code, period_end, value, unit, source,
                     filed_date, knowledge_from, knowledge_to, payload_id)
                VALUES (%s, 'revenue', %s, 100, 'USD', 'edgar', %s, %s, %s, %s)
                """,
            (
                entity_id,
                date(2020, 12, 31),
                date(2021, 2, 1),
                datetime(2021, 6, 1, 13, 30, tzinfo=UTC),
                datetime(2021, 6, 1, 13, 30, tzinfo=UTC),  # equal, not strictly after
                payload_id,
            ),
        )
    db_connection.rollback()


# --- Invariant 5: full lineage (payload_id NOT NULL + FK) -------------------


def test_invariant5_rejects_null_payload_id(db_connection: psycopg.Connection) -> None:
    entity_id = _make_entity(db_connection, "2222222207")

    with pytest.raises(psycopg.errors.NotNullViolation), db_connection.cursor() as cur:
        cur.execute(
            """
                INSERT INTO core.fundamental_fact
                    (entity_id, metric_code, period_end, value, unit, source,
                     filed_date, knowledge_from, knowledge_to, payload_id)
                VALUES (%s, 'revenue', %s, 100, 'USD', 'edgar', %s, %s, 'infinity', NULL)
                """,
            (
                entity_id,
                date(2020, 12, 31),
                date(2021, 2, 1),
                datetime(2021, 2, 2, 13, 30, tzinfo=UTC),
            ),
        )
    db_connection.rollback()


def test_invariant5_rejects_nonexistent_payload_id(db_connection: psycopg.Connection) -> None:
    entity_id = _make_entity(db_connection, "2222222208")

    with pytest.raises(psycopg.errors.ForeignKeyViolation), db_connection.cursor() as cur:
        cur.execute(
            """
                INSERT INTO core.fundamental_fact
                    (entity_id, metric_code, period_end, value, unit, source,
                     filed_date, knowledge_from, knowledge_to, payload_id)
                VALUES (%s, 'revenue', %s, 100, 'USD', 'edgar', %s, %s, 'infinity', 999999999)
                """,
            (
                entity_id,
                date(2020, 12, 31),
                date(2021, 2, 1),
                datetime(2021, 2, 2, 13, 30, tzinfo=UTC),
            ),
        )
    db_connection.rollback()


# --- Same invariants, on core.price_fact (no filed_date; trade_date instead) -


def test_price_fact_invariants_1_3_4_5(db_connection: psycopg.Connection) -> None:
    entity_id = _make_entity(db_connection, "2222222209")
    payload_id = _make_payload_id(db_connection)

    def _insert_price(
        trade_date: date, knowledge_from: object, knowledge_to: object = "infinity"
    ) -> None:
        with db_connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO core.price_fact
                    (entity_id, trade_date, close, source, knowledge_from,
                     knowledge_to, payload_id)
                VALUES (%s, %s, 100, 'yfinance', %s, %s::timestamptz, %s)
                """,
                (entity_id, trade_date, knowledge_from, knowledge_to, payload_id),
            )

    _insert_price(date(2024, 1, 2), datetime(2024, 1, 3, 13, 30, tzinfo=UTC))
    db_connection.commit()

    # invariant 1: second open row for the same (entity, trade_date, source)
    with pytest.raises(psycopg.errors.ExclusionViolation):
        _insert_price(date(2024, 1, 2), datetime(2024, 1, 4, 13, 30, tzinfo=UTC))
    db_connection.rollback()

    # invariant 3: knowledge_from before trade_date
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_price(date(2024, 6, 1), datetime(2024, 1, 1, tzinfo=UTC))
    db_connection.rollback()

    # invariant 4: knowledge_from >= knowledge_to
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_price(
            date(2024, 6, 1),
            datetime(2024, 6, 2, 13, 30, tzinfo=UTC),
            datetime(2024, 6, 2, 13, 30, tzinfo=UTC),
        )
    db_connection.rollback()

    # invariant 5: null payload_id
    with pytest.raises(psycopg.errors.NotNullViolation), db_connection.cursor() as cur:
        cur.execute(
            """
                INSERT INTO core.price_fact
                    (entity_id, trade_date, close, source, knowledge_from, payload_id)
                VALUES (%s, %s, 100, 'yfinance', %s, NULL)
                """,
            (entity_id, date(2024, 6, 1), datetime(2024, 6, 2, 13, 30, tzinfo=UTC)),
        )
    db_connection.rollback()
