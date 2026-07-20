from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
import yaml

from pdw.ops import (
    DEPENDENCY_EDGES,
    FeedStatus,
    SlaDefinition,
    _blast_radius,
    compute_feed_status,
    load_sla,
    render_dependency_dag,
)

_SLA = [
    SlaDefinition(source="edgar", expected_refresh_days=90, max_staleness_days=120),
    SlaDefinition(source="yfinance", expected_refresh_days=1, max_staleness_days=5),
]


def _insert_payload(conn: psycopg.Connection, source: str, fetched_at: datetime) -> None:
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
            VALUES (%s, 'x', '{}'::jsonb, %s, 200, repeat('0', 64), 'x', %s)
            """,
            (source, fetched_at, run_id),
        )
    conn.commit()


def test_load_sla(tmp_path: Path) -> None:
    path = tmp_path / "sla.yaml"
    path.write_text(
        yaml.safe_dump(
            [{"source": "edgar", "expected_refresh_days": 90, "max_staleness_days": 120}]
        )
    )

    result = load_sla(path)

    assert result == [
        SlaDefinition(source="edgar", expected_refresh_days=90, max_staleness_days=120)
    ]


def test_load_sla_rejects_non_list(tmp_path: Path) -> None:
    path = tmp_path / "sla.yaml"
    path.write_text(yaml.safe_dump({"source": "edgar"}))

    with pytest.raises(ValueError, match="must be a non-empty list"):
        load_sla(path)


def test_compute_feed_status_no_data_when_never_fetched(db_connection: psycopg.Connection) -> None:
    sla = [
        SlaDefinition(source="no_such_source_zzz", expected_refresh_days=1, max_staleness_days=5)
    ]

    statuses = compute_feed_status(db_connection, sla)

    assert statuses == [
        FeedStatus(
            source="no_such_source_zzz", last_fetched_at=None, staleness_days=None,
            status="no_data",
        )
    ]


def test_compute_feed_status_ok_when_fresh(db_connection: psycopg.Connection) -> None:
    as_of = datetime(2024, 6, 10, tzinfo=UTC)
    _insert_payload(db_connection, "test_source_ok", as_of - timedelta(hours=6))
    sla = [SlaDefinition(source="test_source_ok", expected_refresh_days=1, max_staleness_days=5)]

    (status,) = compute_feed_status(db_connection, sla, as_of=as_of)

    assert status.status == "ok"
    assert status.staleness_days == pytest.approx(0.25)


def test_compute_feed_status_stale_between_expected_and_max(
    db_connection: psycopg.Connection,
) -> None:
    as_of = datetime(2024, 6, 10, tzinfo=UTC)
    _insert_payload(db_connection, "test_source_stale", as_of - timedelta(days=3))
    sla = [
        SlaDefinition(source="test_source_stale", expected_refresh_days=1, max_staleness_days=5)
    ]

    (status,) = compute_feed_status(db_connection, sla, as_of=as_of)

    assert status.status == "stale"


def test_compute_feed_status_breach_past_max_staleness(db_connection: psycopg.Connection) -> None:
    as_of = datetime(2024, 6, 10, tzinfo=UTC)
    _insert_payload(db_connection, "test_source_breach", as_of - timedelta(days=10))
    sla = [
        SlaDefinition(source="test_source_breach", expected_refresh_days=1, max_staleness_days=5)
    ]

    (status,) = compute_feed_status(db_connection, sla, as_of=as_of)

    assert status.status == "breach"


def test_blast_radius_includes_every_transitive_downstream_node() -> None:
    radius = _blast_radius("edgar")

    assert "stg.edgar_fundamental_fact" in radius
    assert "core.fundamental_fact" in radius
    assert "dq: balance_sheet_identity" in radius
    assert "M7 backtest" in radius  # transitive: edgar -> ... -> fundamentals reader -> backtest
    assert "core.price_fact" not in radius  # not downstream of edgar at all


def test_blast_radius_is_empty_for_a_leaf_node() -> None:
    assert _blast_radius("M7 backtest") == set()


def test_render_dependency_dag_marks_breaching_feed_and_its_downstream() -> None:
    statuses = [
        FeedStatus(source="edgar", last_fetched_at=None, staleness_days=200.0, status="breach"),
        FeedStatus(source="yfinance", last_fetched_at=None, staleness_days=0.1, status="ok"),
    ]

    dag = render_dependency_dag(statuses)

    assert "flowchart LR" in dag
    assert '"edgar"' in dag
    # every DAG node must appear at least once as a source or target
    for source, targets in DEPENDENCY_EDGES.items():
        assert f'"{source}"' in dag
        for target in targets:
            assert f'"{target}"' in dag
