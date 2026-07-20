from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest

from pdw.ingest import ingest
from pdw.sources import FetchResult


class _FakeSource:
    def __init__(self, name: str, results: list[FetchResult]) -> None:
        self.name = name
        self._results = results

    def fetch_universe(self, tickers: list[str]) -> Iterator[FetchResult]:
        yield from self._results


def _result(body: bytes, ticker: str = "AAPL") -> FetchResult:
    return FetchResult(
        endpoint="test",
        request_params={"ticker": ticker},
        fetched_at=datetime.now(UTC),
        http_status=200,
        body=body,
    )


def test_ingest_writes_one_raw_payload_row_per_fetch_result(
    db_connection: psycopg.Connection,
) -> None:
    source = _FakeSource("fake-write", [_result(b"aaa", "AAPL"), _result(b"bbb", "MSFT")])

    ingest(db_connection, source, ["AAPL", "MSFT"])

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT endpoint, content_sha256 FROM raw.payload "
            "WHERE source = %s ORDER BY payload_id",
            (source.name,),
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    assert rows[0] == ("test", hashlib.sha256(b"aaa").hexdigest())
    assert rows[1] == ("test", hashlib.sha256(b"bbb").hexdigest())


def test_ingest_records_a_successful_pipeline_run(db_connection: psycopg.Connection) -> None:
    source = _FakeSource("fake-run", [_result(b"ccc")])

    ingest(db_connection, source, ["AAPL"])

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT status, rows_in, rows_out FROM ops.pipeline_run WHERE pipeline = %s",
            (f"ingest:{source.name}",),
        )
        row = cur.fetchone()

    assert row == ("success", 1, 1)


def test_second_identical_run_adds_rows_but_no_new_distinct_hashes(
    db_connection: psycopg.Connection,
) -> None:
    source_name = "fake-dedup"

    ingest(db_connection, _FakeSource(source_name, [_result(b"same-content")]), ["AAPL"])
    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.payload WHERE source = %s", (source_name,))
        (count_after_first,) = cur.fetchone()
        cur.execute(
            "SELECT count(DISTINCT content_sha256) FROM raw.payload WHERE source = %s",
            (source_name,),
        )
        (distinct_after_first,) = cur.fetchone()

    ingest(db_connection, _FakeSource(source_name, [_result(b"same-content")]), ["AAPL"])
    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.payload WHERE source = %s", (source_name,))
        (count_after_second,) = cur.fetchone()
        cur.execute(
            "SELECT count(DISTINCT content_sha256) FROM raw.payload WHERE source = %s",
            (source_name,),
        )
        (distinct_after_second,) = cur.fetchone()

    assert count_after_second > count_after_first
    assert distinct_after_second == distinct_after_first == 1


def test_ingest_marks_pipeline_run_failed_on_error(db_connection: psycopg.Connection) -> None:
    class _BrokenSource:
        name = "fake-broken"

        def fetch_universe(self, tickers: list[str]) -> Iterator[FetchResult]:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        ingest(db_connection, _BrokenSource(), ["AAPL"])

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT status, error->>'message' FROM ops.pipeline_run "
            "WHERE pipeline = 'ingest:fake-broken'"
        )
        row = cur.fetchone()

    assert row == ("failed", "boom")


def test_ingest_marks_pipeline_run_failed_when_the_db_itself_errors(
    db_connection: psycopg.Connection,
) -> None:
    """A DB-level error (not just an application exception) must still land
    the pipeline_run row as 'failed', not leave it stuck at 'running'.

    A NOT NULL violation aborts the connection's transaction; pipeline_run's
    failure handler must roll back before it can record anything.
    """
    bad_result = FetchResult(
        endpoint=None,  # type: ignore[arg-type]
        request_params={"ticker": "AAPL"},
        fetched_at=datetime.now(UTC),
        http_status=200,
        body=b"x",
    )
    source = _FakeSource("fake-db-abort", [bad_result])

    with pytest.raises(psycopg.errors.NotNullViolation):
        ingest(db_connection, source, ["AAPL"])

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT status FROM ops.pipeline_run WHERE pipeline = 'ingest:fake-db-abort'"
        )
        row = cur.fetchone()

    assert row == ("failed",)


def test_raw_payload_is_append_only(db_connection: psycopg.Connection) -> None:
    ingest(db_connection, _FakeSource("fake-append-only", [_result(b"immutable")]), ["AAPL"])

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT payload_id FROM raw.payload WHERE source = 'fake-append-only'",
        )
        (payload_id,) = cur.fetchone()

        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "UPDATE raw.payload SET http_status = 0 WHERE payload_id = %s", (payload_id,)
            )
    db_connection.rollback()
