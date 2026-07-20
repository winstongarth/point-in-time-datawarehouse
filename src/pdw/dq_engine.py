from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from pdw.db import pipeline_run
from pdw.dq_checks import (
    CheckResult,
    check_balance_sheet_identity,
    check_cross_vendor_price,
    check_payload_freshness,
    check_period_coverage_gaps,
    check_price_staleness,
    check_return_outliers,
    check_revenue_sanity,
    check_tag_switches,
    load_reconciliation_rules,
)


@dataclass
class DqRunSummary:
    total_checks: int
    passed: int
    failed: int
    by_severity_fail: dict[str, int]


def run_all_checks(conn: psycopg.Connection, reconciliation_config: Path) -> DqRunSummary:
    """Run all 8 required checks (CLAUDE.md 7) and update the dq.exception
    lifecycle for each. Every check's every result is written to
    dq.check_result, including passes."""
    rules = load_reconciliation_rules(reconciliation_config)

    with pipeline_run(conn, pipeline="dq") as run:
        results: list[CheckResult] = []
        results.extend(check_cross_vendor_price(conn, rules))
        results.extend(check_balance_sheet_identity(conn))
        results.extend(check_revenue_sanity(conn))
        results.extend(check_period_coverage_gaps(conn))
        results.extend(check_price_staleness(conn))
        results.extend(check_return_outliers(conn))
        results.extend(check_payload_freshness(conn))
        results.extend(check_tag_switches(conn))

        run.rows_in = len(results)
        for result in results:
            check_id = _write_check_result(conn, result, run.run_id)
            _update_exception_lifecycle(conn, result, check_id)
            run.rows_out += 1

    conn.commit()

    failed = [r for r in results if r.status == "fail"]
    return DqRunSummary(
        total_checks=len(results),
        passed=len(results) - len(failed),
        failed=len(failed),
        by_severity_fail=dict(Counter(r.severity for r in failed)),
    )


def _write_check_result(conn: psycopg.Connection, result: CheckResult, run_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dq.check_result
                (check_name, dataset, run_id, severity, status, observed, expected)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING check_id
            """,
            (
                result.check_name,
                result.dataset,
                run_id,
                result.severity,
                result.status,
                Jsonb(result.observed),
                Jsonb(result.expected),
            ),
        )
        row = cur.fetchone()
        assert row is not None
        check_id: int = row[0]
        return check_id


def _update_exception_lifecycle(
    conn: psycopg.Connection, result: CheckResult, check_id: int
) -> None:
    """Open a new exception on a failure with none already open/in-triage
    for this (check_name, dimension_key); auto-close one when the check
    passes again. A recurring failure with an exception already open is a
    no-op - it's still failing, nothing changes."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ex.exception_id FROM dq.exception ex
            JOIN dq.check_result cr ON cr.check_id = ex.check_id
            WHERE cr.check_name = %s AND ex.dimension_key = %s
                  AND ex.status IN ('open', 'triage')
            """,
            (result.check_name, result.dimension_key),
        )
        existing = cur.fetchone()

        if result.status == "fail" and existing is None:
            cur.execute(
                """
                INSERT INTO dq.exception (check_id, entity_id, dimension_key, severity, status)
                VALUES (%s, %s, %s, %s, 'open')
                """,
                (check_id, result.entity_id, result.dimension_key, result.severity),
            )
        elif result.status == "pass" and existing is not None:
            cur.execute(
                """
                UPDATE dq.exception
                SET status = 'closed', closed_at = now(),
                    resolution_note = 'auto-resolved: check passed again'
                WHERE exception_id = %s
                """,
                (existing[0],),
            )


def triage_exception(conn: psycopg.Connection, exception_id: int, note: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE dq.exception SET status = 'triage', resolution_note = %s
            WHERE exception_id = %s AND status = 'open'
            """,
            (note, exception_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"no open exception with id {exception_id}")
    conn.commit()


def resolve_exception(conn: psycopg.Connection, exception_id: int, note: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE dq.exception SET status = 'closed', closed_at = now(), resolution_note = %s
            WHERE exception_id = %s AND status IN ('open', 'triage')
            """,
            (note, exception_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"no open/triage exception with id {exception_id}")
    conn.commit()
