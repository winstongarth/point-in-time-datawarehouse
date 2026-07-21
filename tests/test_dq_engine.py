from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
import yaml

from pdw.dq_engine import resolve_exception, run_all_checks, triage_exception

# One real rule, matching config/reconciliation.yaml's shape - avoids
# depending on that file's actual path from the test's working directory,
# while still exercising check_cross_vendor_price's own "no comparable
# rows yet" vacuous-pass result (every check writes a result
# every run, including passes).
_RULES: list[dict[str, object]] = [
    {
        "name": "price_close_cross_vendor",
        "left": {"source": "yfinance", "field": "close"},
        "right": {"source": "tiingo", "field": "close"},
        "grain": ["entity_id", "trade_date"],
        "tolerance": {"type": "relative", "value": 0.001},
        "severity": "WARN",
        "escalate_if": {"consecutive_days": 3, "severity": "BREAK"},
    }
]


@pytest.fixture
def reconciliation_config(tmp_path: Path) -> Path:
    path = tmp_path / "reconciliation.yaml"
    path.write_text(yaml.safe_dump(_RULES))
    return path


def test_run_all_checks_writes_a_result_for_every_check(
    db_connection: psycopg.Connection, reconciliation_config: Path
) -> None:
    summary = run_all_checks(db_connection, reconciliation_config)

    assert summary.total_checks >= 8  # at least one result per check, most vacuous passes
    with db_connection.cursor() as cur:
        cur.execute("SELECT DISTINCT check_name FROM dq.check_result")
        names = {row[0] for row in cur.fetchall()}

    assert names == {
        "price_close_cross_vendor",
        "balance_sheet_identity",
        "revenue_sanity",
        "period_coverage_gaps",
        "price_staleness",
        "return_outliers",
        "payload_freshness",
        "tag_switches",
    }


def test_failing_check_opens_an_exception(
    db_connection: psycopg.Connection, reconciliation_config: Path
) -> None:
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO core.entity (cik, name) VALUES ('6666666601', 'Test') "
            "RETURNING entity_id"
        )
        row = cur.fetchone()
        assert row is not None
        entity_id = row[0]
        cur.execute("INSERT INTO ops.pipeline_run (pipeline) VALUES ('test') RETURNING run_id")
        row = cur.fetchone()
        assert row is not None
        run_id = row[0]
        cur.execute(
            """
            INSERT INTO raw.payload (source, endpoint, request_params, fetched_at,
                                      http_status, content_sha256, body, run_id)
            VALUES ('yfinance', 'history', '{}'::jsonb, now(), 200, repeat('0', 64), 'x', %s)
            RETURNING payload_id
            """,
            (run_id,),
        )
        row = cur.fetchone()
        assert row is not None
        payload_id = row[0]
        cur.execute(
            """
            INSERT INTO core.price_fact (entity_id, trade_date, close, source,
                                          knowledge_from, payload_id)
            VALUES (%s, '2020-01-01', 100, 'yfinance', now(), %s)
            """,
            (entity_id, payload_id),
        )
    db_connection.commit()

    run_all_checks(db_connection, reconciliation_config)

    with db_connection.cursor() as cur:
        cur.execute(
            """
            SELECT ex.status FROM dq.exception ex
            JOIN dq.check_result cr ON cr.check_id = ex.check_id
            WHERE cr.check_name = 'price_staleness' AND ex.entity_id = %s
            """,
            (entity_id,),
        )
        row = cur.fetchone()

    assert row is not None
    assert row[0] == "open"


def test_repeated_failure_does_not_duplicate_the_exception(
    db_connection: psycopg.Connection, reconciliation_config: Path
) -> None:
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO core.entity (cik, name) VALUES ('6666666602', 'Test') "
            "RETURNING entity_id"
        )
        row = cur.fetchone()
        assert row is not None
        entity_id = row[0]
        cur.execute("INSERT INTO ops.pipeline_run (pipeline) VALUES ('test') RETURNING run_id")
        row = cur.fetchone()
        assert row is not None
        run_id = row[0]
        cur.execute(
            """
            INSERT INTO raw.payload (source, endpoint, request_params, fetched_at,
                                      http_status, content_sha256, body, run_id)
            VALUES ('yfinance', 'history', '{}'::jsonb, now(), 200, repeat('0', 64), 'x', %s)
            RETURNING payload_id
            """,
            (run_id,),
        )
        row = cur.fetchone()
        assert row is not None
        payload_id = row[0]
        cur.execute(
            """
            INSERT INTO core.price_fact (entity_id, trade_date, close, source,
                                          knowledge_from, payload_id)
            VALUES (%s, '2020-01-01', 100, 'yfinance', now(), %s)
            """,
            (entity_id, payload_id),
        )
    db_connection.commit()

    run_all_checks(db_connection, reconciliation_config)
    run_all_checks(db_connection, reconciliation_config)

    with db_connection.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM dq.exception ex
            JOIN dq.check_result cr ON cr.check_id = ex.check_id
            WHERE cr.check_name = 'price_staleness' AND ex.entity_id = %s
            """,
            (entity_id,),
        )
        (count,) = cur.fetchone()

    assert count == 1


def test_check_passing_again_auto_closes_the_exception(
    db_connection: psycopg.Connection, reconciliation_config: Path
) -> None:
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO core.entity (cik, name) VALUES ('6666666603', 'Test') "
            "RETURNING entity_id"
        )
        row = cur.fetchone()
        assert row is not None
        entity_id = row[0]
        cur.execute("INSERT INTO ops.pipeline_run (pipeline) VALUES ('test') RETURNING run_id")
        row = cur.fetchone()
        assert row is not None
        run_id = row[0]
        cur.execute(
            """
            INSERT INTO raw.payload (source, endpoint, request_params, fetched_at,
                                      http_status, content_sha256, body, run_id)
            VALUES ('yfinance', 'history', '{}'::jsonb, now(), 200, repeat('0', 64), 'x', %s)
            RETURNING payload_id
            """,
            (run_id,),
        )
        row = cur.fetchone()
        assert row is not None
        payload_id = row[0]
        cur.execute(
            """
            INSERT INTO core.price_fact (entity_id, trade_date, close, source,
                                          knowledge_from, payload_id)
            VALUES (%s, '2020-01-01', 100, 'yfinance', now(), %s)
            """,
            (entity_id, payload_id),
        )
    db_connection.commit()
    run_all_checks(db_connection, reconciliation_config)

    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO core.price_fact (entity_id, trade_date, close, source, "
            "knowledge_from, payload_id) VALUES (%s, current_date, 100, 'yfinance', now(), %s)",
            (entity_id, payload_id),
        )
    db_connection.commit()
    run_all_checks(db_connection, reconciliation_config)

    with db_connection.cursor() as cur:
        cur.execute(
            """
            SELECT ex.status, ex.closed_at IS NOT NULL FROM dq.exception ex
            JOIN dq.check_result cr ON cr.check_id = ex.check_id
            WHERE cr.check_name = 'price_staleness' AND ex.entity_id = %s
            """,
            (entity_id,),
        )
        row = cur.fetchone()

    assert row == ("closed", True)


def test_triage_then_resolve_lifecycle(
    db_connection: psycopg.Connection, reconciliation_config: Path
) -> None:
    with db_connection.cursor() as cur:
        cur.execute("INSERT INTO ops.pipeline_run (pipeline) VALUES ('test') RETURNING run_id")
        row = cur.fetchone()
        assert row is not None
        run_id = row[0]
        cur.execute(
            """
            INSERT INTO dq.check_result (check_name, dataset, run_id, severity, status)
            VALUES ('manual_test_check', 'core.entity', %s, 'WARN', 'fail')
            RETURNING check_id
            """,
            (run_id,),
        )
        row = cur.fetchone()
        assert row is not None
        check_id = row[0]
        cur.execute(
            """
            INSERT INTO dq.exception (check_id, dimension_key, severity, status)
            VALUES (%s, 'manual-test-dim', 'WARN', 'open')
            RETURNING exception_id
            """,
            (check_id,),
        )
        row = cur.fetchone()
        assert row is not None
        exception_id = row[0]
    db_connection.commit()

    triage_exception(db_connection, exception_id, "investigating")
    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT status, resolution_note FROM dq.exception WHERE exception_id = %s",
            (exception_id,),
        )
        assert cur.fetchone() == ("triage", "investigating")

    resolve_exception(db_connection, exception_id, "confirmed benign, closing")
    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT status, resolution_note, closed_at IS NOT NULL FROM dq.exception "
            "WHERE exception_id = %s",
            (exception_id,),
        )
        assert cur.fetchone() == ("closed", "confirmed benign, closing", True)


def test_triage_nonexistent_exception_raises(
    db_connection: psycopg.Connection,
) -> None:
    with pytest.raises(ValueError, match="no open exception"):
        triage_exception(db_connection, 999999, "note")


def test_null_price_on_one_vendor_writes_without_crashing(
    db_connection: psycopg.Connection, reconciliation_config: Path
) -> None:
    """Regression: check_cross_vendor_price's null-handling branch once
    passed a raw Decimal straight into `observed`, which crashed with
    `TypeError: Object of type Decimal is not JSON serializable` the moment
    _write_check_result tried to Jsonb-encode it. Only surfaced when routed
    through the full engine (a check-function-only test never encodes
    anything), so this test seeds the failure through run_all_checks itself.
    """
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO core.entity (cik, name) VALUES ('6666666604', 'Test') "
            "RETURNING entity_id"
        )
        row = cur.fetchone()
        assert row is not None
        entity_id = row[0]
        cur.execute("INSERT INTO ops.pipeline_run (pipeline) VALUES ('test') RETURNING run_id")
        row = cur.fetchone()
        assert row is not None
        run_id = row[0]
        cur.execute(
            """
            INSERT INTO raw.payload (source, endpoint, request_params, fetched_at,
                                      http_status, content_sha256, body, run_id)
            VALUES ('yfinance', 'history', '{}'::jsonb, now(), 200, repeat('1', 64), 'x', %s)
            RETURNING payload_id
            """,
            (run_id,),
        )
        row = cur.fetchone()
        assert row is not None
        payload_id = row[0]
        # yfinance has a real close; tiingo has a row for the same
        # (entity_id, trade_date) but a NULL close - the exact shape that
        # crashed before the float()-cast fix.
        cur.execute(
            """
            INSERT INTO core.price_fact (entity_id, trade_date, close, source,
                                          knowledge_from, payload_id)
            VALUES (%s, '2020-01-01', 100, 'yfinance', now(), %s),
                   (%s, '2020-01-01', NULL, 'tiingo', now(), %s)
            """,
            (entity_id, payload_id, entity_id, payload_id),
        )
    db_connection.commit()

    summary = run_all_checks(db_connection, reconciliation_config)

    assert summary.failed >= 1
    with db_connection.cursor() as cur:
        cur.execute(
            """
            SELECT cr.observed FROM dq.check_result cr
            JOIN dq.exception ex ON ex.check_id = cr.check_id
            WHERE cr.check_name = 'price_close_cross_vendor' AND ex.entity_id = %s
            """,
            (entity_id,),
        )
        (observed,) = cur.fetchone()

    assert observed == {"left": 100.0, "right": None}
