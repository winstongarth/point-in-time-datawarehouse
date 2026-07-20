from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pdw.metric_map import load_metric_map
from pdw.parse import run_parse

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "edgar"
METRIC_MAP_PATH = Path(__file__).resolve().parent.parent / "config" / "metric_map.yaml"


def _insert_payload(
    conn: psycopg.Connection,
    *,
    source: str,
    endpoint: str,
    request_params: dict[str, object],
    body: bytes,
) -> int:
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
            VALUES (%s, %s, %s, %s, 200, %s, %s, %s)
            RETURNING payload_id
            """,
            (
                source,
                endpoint,
                Jsonb(request_params),
                datetime.now(UTC),
                hashlib.sha256(body).hexdigest(),
                body,
                run_id,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        payload_id: int = row[0]
    conn.commit()
    return payload_id


def _seed_aapl(conn: psycopg.Connection, ticker: str = "AAPL") -> None:
    ticker_map_body = (FIXTURES / "ticker_map_sample.json").read_bytes()
    if ticker != "AAPL":
        # Rewrite the trimmed fixture map so AAPL's CIK now resolves under a
        # different ticker - simulates SEC's map reflecting a reassignment.
        data = json.loads(ticker_map_body)
        for entry in data.values():
            if entry["ticker"] == "AAPL":
                entry["ticker"] = ticker
        ticker_map_body = json.dumps(data).encode("utf-8")

    companyfacts_body = (FIXTURES / "aapl_companyfacts_sample.json").read_bytes()

    _insert_payload(
        conn, source="edgar", endpoint="ticker_map", request_params={}, body=ticker_map_body
    )
    _insert_payload(
        conn,
        source="edgar",
        endpoint="companyfacts",
        request_params={"ticker": ticker, "cik": "0000320193"},
        body=companyfacts_body,
    )


def test_run_parse_populates_stg_and_entity(db_connection: psycopg.Connection) -> None:
    _seed_aapl(db_connection)
    mapping = load_metric_map(METRIC_MAP_PATH)

    summary, facts, ticker_by_cik = run_parse(db_connection, mapping, ["AAPL"])

    assert summary.entities_parsed == 1
    assert summary.facts_written == len(facts) > 0
    assert ticker_by_cik == {"0000320193": "AAPL"}

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM stg.edgar_fundamental_fact")
        (stg_count,) = cur.fetchone()
        cur.execute("SELECT cik, name FROM core.entity WHERE cik = '0000320193'")
        entity_row = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM core.entity_ticker "
            "WHERE ticker = 'AAPL' AND knowledge_to = 'infinity'"
        )
        (open_ticker_count,) = cur.fetchone()

    assert stg_count == summary.facts_written
    assert entity_row == ("0000320193", "Apple Inc.")
    assert open_ticker_count == 1


def test_first_ticker_mapping_is_backdated_to_the_sentinel_not_ingestion_time(
    db_connection: psycopg.Connection,
) -> None:
    """Regression test (M5 amendment, 2026-07-20): a brand-new entity's
    first ticker mapping must open at the fixed historical sentinel, not
    "when we happened to fetch the ticker map" - otherwise PointInTimeReader
    returns nothing for any as_of before this project's own first ingestion
    run, which would break M7's historical rebalance-date reads entirely.
    """
    _seed_aapl(db_connection)
    mapping = load_metric_map(METRIC_MAP_PATH)

    run_parse(db_connection, mapping, ["AAPL"])

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT knowledge_from FROM core.entity_ticker WHERE ticker = 'AAPL'"
        )
        (knowledge_from,) = cur.fetchone()

    assert knowledge_from == datetime(2000, 1, 1, tzinfo=UTC)


def test_run_parse_still_creates_entity_for_ticker_with_zero_facts(
    db_connection: psycopg.Connection,
) -> None:
    """Regression test for a real case found live (2026-07-20): XOM's ticker
    currently resolves to a freshly-reorganized holding-company CIK whose
    companyfacts response has entityName but zero us-gaap facts (only a
    registration-statement fee filing under a different taxonomy). Entity/
    ticker mapping must not be skipped just because there are no fundamentals
    yet - it's a CLAUDE.md M3 deliverable in its own right.
    """
    ticker_map_body = json.dumps(
        {"0": {"cik_str": 9999999999, "ticker": "ZEROFACTS", "title": "Zero Facts Corp"}}
    ).encode("utf-8")
    companyfacts_body = json.dumps(
        {
            "cik": 9999999999,
            "entityName": "ZERO FACTS CORP",
            "facts": {"ffd": {"NetFeeAmt": {"units": {"USD": []}}}},
        }
    ).encode("utf-8")

    _insert_payload(
        db_connection,
        source="edgar",
        endpoint="ticker_map",
        request_params={},
        body=ticker_map_body,
    )
    _insert_payload(
        db_connection,
        source="edgar",
        endpoint="companyfacts",
        request_params={"ticker": "ZEROFACTS", "cik": "9999999999"},
        body=companyfacts_body,
    )

    mapping = load_metric_map(METRIC_MAP_PATH)
    summary, facts, ticker_by_cik = run_parse(db_connection, mapping, ["ZEROFACTS"])

    assert summary.entities_parsed == 1
    assert facts == []
    assert ticker_by_cik == {"9999999999": "ZEROFACTS"}

    with db_connection.cursor() as cur:
        cur.execute("SELECT cik, name FROM core.entity WHERE cik = '9999999999'")
        entity_row = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM core.entity_ticker "
            "WHERE ticker = 'ZEROFACTS' AND knowledge_to = 'infinity'"
        )
        (open_ticker_count,) = cur.fetchone()

    assert entity_row == ("9999999999", "ZERO FACTS CORP")
    assert open_ticker_count == 1


def test_run_parse_reports_ticker_with_no_cik(db_connection: psycopg.Connection) -> None:
    _seed_aapl(db_connection)
    mapping = load_metric_map(METRIC_MAP_PATH)

    summary, _facts, _ticker_by_cik = run_parse(
        db_connection, mapping, ["AAPL", "DEFINITELY_NOT_A_REAL_TICKER"]
    )

    assert summary.tickers_without_cik == ("DEFINITELY_NOT_A_REAL_TICKER",)


def test_run_parse_is_idempotent_for_entities_and_tickers(
    db_connection: psycopg.Connection,
) -> None:
    _seed_aapl(db_connection)
    mapping = load_metric_map(METRIC_MAP_PATH)

    run_parse(db_connection, mapping, ["AAPL"])
    run_parse(db_connection, mapping, ["AAPL"])

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.entity WHERE cik = '0000320193'")
        (entity_count,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM core.entity_ticker WHERE ticker = 'AAPL'")
        (ticker_row_count,) = cur.fetchone()

    assert entity_count == 1
    assert ticker_row_count == 1  # second run is a no-op, not a new open row


def test_ticker_reassignment_closes_old_row_and_opens_new(
    db_connection: psycopg.Connection,
) -> None:
    _seed_aapl(db_connection, ticker="AAPL")
    mapping = load_metric_map(METRIC_MAP_PATH)
    run_parse(db_connection, mapping, ["AAPL"])

    _seed_aapl(db_connection, ticker="AAPLX")
    run_parse(db_connection, mapping, ["AAPLX"])

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM core.entity_ticker "
            "WHERE ticker = 'AAPL' AND knowledge_to < 'infinity'"
        )
        (closed_old,) = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM core.entity_ticker "
            "WHERE ticker = 'AAPLX' AND knowledge_to = 'infinity'"
        )
        (open_new,) = cur.fetchone()

    assert closed_old == 1
    assert open_new == 1


def test_entity_ticker_rejects_overlapping_ticker_for_a_different_entity(
    db_connection: psycopg.Connection,
) -> None:
    """core.entity_ticker's EXCLUDE constraint (invariant-style, mirroring
    CLAUDE.md 5 #1) must reject two entities holding the same ticker at the
    same time."""
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO core.entity (cik, name) VALUES ('1111111111', 'One') RETURNING entity_id"
        )
        (entity_a,) = cur.fetchone()
        cur.execute(
            "INSERT INTO core.entity (cik, name) VALUES ('2222222222', 'Two') RETURNING entity_id"
        )
        (entity_b,) = cur.fetchone()
        cur.execute(
            "INSERT INTO core.entity_ticker (entity_id, ticker, knowledge_from) "
            "VALUES (%s, 'DUP', now())",
            (entity_a,),
        )
        db_connection.commit()

        with pytest.raises(psycopg.errors.ExclusionViolation):
            cur.execute(
                "INSERT INTO core.entity_ticker (entity_id, ticker, knowledge_from) "
                "VALUES (%s, 'DUP', now())",
                (entity_b,),
            )
    db_connection.rollback()
