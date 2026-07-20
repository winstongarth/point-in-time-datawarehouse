from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from pdw.config import get_settings

logger = logging.getLogger(__name__)


@contextmanager
def get_connection() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(get_settings().database_url)
    try:
        yield conn
    finally:
        conn.close()


@dataclass
class PipelineRunHandle:
    """Mutable counters an ingestion/parse/load step fills in as it runs.

    `run_id` is what every raw.payload / dq.check_result row traces back to
    (CLAUDE.md's ops.pipeline_run), so it's available as soon as the run
    starts rather than only once it finishes.
    """

    run_id: int
    rows_in: int = 0
    rows_out: int = 0


@contextmanager
def pipeline_run(conn: psycopg.Connection, pipeline: str) -> Iterator[PipelineRunHandle]:
    """Open an ops.pipeline_run row, yield a handle, close it on exit.

    Closes with status='success' if the block completes, or status='failed'
    with the exception recorded in `error` if it raises — the exception is
    then re-raised so the caller still sees (and fails loudly on) the error.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.pipeline_run (pipeline) VALUES (%s) RETURNING run_id",
            (pipeline,),
        )
        row = cur.fetchone()
        assert row is not None
        run_id: int = row[0]
    conn.commit()

    handle = PipelineRunHandle(run_id=run_id)
    try:
        yield handle
    except Exception as exc:
        logger.exception("pipeline run failed", extra={"run_id": run_id, "pipeline": pipeline})
        # The failure that brought us here may itself have been a DB error,
        # which leaves the connection's transaction aborted; every statement
        # on it (including the UPDATE below) would otherwise fail too until
        # that's cleared.
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ops.pipeline_run
                SET ended_at = now(), status = 'failed', rows_in = %s, rows_out = %s, error = %s
                WHERE run_id = %s
                """,
                (
                    handle.rows_in,
                    handle.rows_out,
                    Jsonb({"type": type(exc).__name__, "message": str(exc)}),
                    run_id,
                ),
            )
        conn.commit()
        raise
    else:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ops.pipeline_run
                SET ended_at = now(), status = 'success', rows_in = %s, rows_out = %s
                WHERE run_id = %s
                """,
                (handle.rows_in, handle.rows_out, run_id),
            )
        conn.commit()
