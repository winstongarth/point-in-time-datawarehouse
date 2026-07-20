from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import yaml


@dataclass(frozen=True)
class SlaDefinition:
    source: str
    expected_refresh_days: int
    max_staleness_days: int


def load_sla(path: Path) -> list[SlaDefinition]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, list) or not data:
        raise ValueError(f"SLA config {path} must be a non-empty list")
    return [
        SlaDefinition(
            source=item["source"],
            expected_refresh_days=item["expected_refresh_days"],
            max_staleness_days=item["max_staleness_days"],
        )
        for item in data
    ]


@dataclass(frozen=True)
class FeedStatus:
    source: str
    last_fetched_at: datetime | None
    staleness_days: float | None
    status: str  # "ok" | "stale" | "breach" | "no_data"


def compute_feed_status(
    conn: psycopg.Connection, sla_definitions: list[SlaDefinition], as_of: datetime | None = None
) -> list[FeedStatus]:
    """Per-feed freshness against its own SLA (CLAUDE.md 8, M8: "`pdw ops
    status` shows per-feed freshness against SLA"). A feed with zero
    `raw.payload` rows at all is "no_data", not "breach" - those are
    different failure modes (a feed that's never been run vs. one that's
    stopped running) and the M6 postmortem is exactly about that
    distinction going unnoticed - see docs/postmortems.md.
    """
    as_of = as_of or datetime.now(UTC)
    statuses: list[FeedStatus] = []
    for sla in sla_definitions:
        with conn.cursor() as cur:
            cur.execute("SELECT max(fetched_at) FROM raw.payload WHERE source = %s", (sla.source,))
            row = cur.fetchone()
            last_fetched_at = row[0] if row else None

        if last_fetched_at is None:
            statuses.append(FeedStatus(sla.source, None, None, "no_data"))
            continue

        staleness_days = (as_of - last_fetched_at).total_seconds() / 86400
        if staleness_days > sla.max_staleness_days:
            status = "breach"
        elif staleness_days > sla.expected_refresh_days:
            status = "stale"
        else:
            status = "ok"
        statuses.append(FeedStatus(sla.source, last_fetched_at, staleness_days, status))
    return statuses


# The pipeline's dependency structure (CLAUDE.md 8, M8: "a dependency DAG
# showing blast radius of a feed failure") - static and hand-maintained,
# not derived from the schema, because "which checks/consumers read this
# table" isn't something information_schema can answer (same reasoning as
# pdw.dictionary's hand-curated notes). Update this alongside any change
# that adds a new consumer of core.fundamental_fact/core.price_fact.
DEPENDENCY_EDGES: dict[str, list[str]] = {
    "edgar": ["stg.edgar_fundamental_fact"],
    "stg.edgar_fundamental_fact": ["core.fundamental_fact"],
    "core.fundamental_fact": [
        "PointInTimeReader.fundamentals",
        "dq: balance_sheet_identity",
        "dq: revenue_sanity",
        "dq: period_coverage_gaps",
        "dq: tag_switches",
    ],
    "yfinance": ["core.price_fact"],
    "tiingo": ["core.price_fact"],
    "core.price_fact": [
        "PointInTimeReader.prices",
        "dq: price_close_cross_vendor",
        "dq: price_staleness",
        "dq: return_outliers",
    ],
    "PointInTimeReader.fundamentals": ["M7 backtest"],
    "PointInTimeReader.prices": ["M7 backtest"],
}

_STATUS_FILL = {
    "breach": "#f8d7da,stroke:#c0392b",
    "stale": "#fff3cd,stroke:#b7950b",
    "ok": "#d4edda,stroke:#27ae60",
    "no_data": "#e2e3e5,stroke:#6c757d",
}
_AT_RISK_FILL = "#fde3cf,stroke:#e67e22,stroke-dasharray: 4 2"


def _blast_radius(source: str) -> set[str]:
    """Every node transitively downstream of `source` - the "blast radius"
    if that feed were to fail right now."""
    visited: set[str] = set()
    frontier = [source]
    while frontier:
        node = frontier.pop()
        for downstream in DEPENDENCY_EDGES.get(node, []):
            if downstream not in visited:
                visited.add(downstream)
                frontier.append(downstream)
    return visited


def render_dependency_dag(statuses: list[FeedStatus]) -> str:
    status_by_source = {s.source: s.status for s in statuses}
    at_risk: set[str] = set()
    for source, status in status_by_source.items():
        if status in ("breach", "stale"):
            at_risk |= _blast_radius(source)

    lines = ["flowchart LR"]
    all_nodes = set(DEPENDENCY_EDGES) | {n for edges in DEPENDENCY_EDGES.values() for n in edges}
    node_ids = {name: f"n{i}" for i, name in enumerate(sorted(all_nodes))}

    for source, targets in DEPENDENCY_EDGES.items():
        for target in targets:
            lines.append(
                f'    {node_ids[source]}["{source}"] --> {node_ids[target]}["{target}"]'
            )

    for name, node_id in node_ids.items():
        if name in status_by_source:
            lines.append(f"    style {node_id} fill:{_STATUS_FILL[status_by_source[name]]}")
        elif name in at_risk:
            lines.append(f"    style {node_id} fill:{_AT_RISK_FILL}")

    return "\n".join(lines) + "\n"
