from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

# Approximates "next trading day open" without a full NYSE
# holiday calendar: weekends are skipped, US market holidays are not - a
# documented simplification (see docs/limitations.md). Market open is
# approximated as 13:30 UTC (9:30am US Eastern) year-round, ignoring the
# EST/EDT switch - also documented. Getting the exact minute right needs a
# real trading calendar, which is out of scope here; what matters for the
# invariant this supports is that knowledge never precedes the filing/trade
# date, which holds regardless of these simplifications.
_MARKET_OPEN_UTC_HOUR = 13
_MARKET_OPEN_UTC_MINUTE = 30

_SATURDAY = 5


@dataclass(frozen=True)
class SourceAvailability:
    availability_lag_days: int


def load_source_availability(path: Path) -> dict[str, SourceAvailability]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or not data:
        raise ValueError(f"source availability config {path} is empty or not a mapping")
    return {
        name: SourceAvailability(availability_lag_days=body["availability_lag_days"])
        for name, body in data.items()
    }


def _next_business_day(d: date) -> date:
    while d.weekday() >= _SATURDAY:
        d += timedelta(days=1)
    return d


def compute_knowledge_from(base_date: date, lag_days: int) -> datetime:
    """`base_date` (a filing or trade date) plus `lag_days` calendar days,
    rolled forward to the next weekday if that lands on a weekend, at
    approximate market open."""
    candidate = _next_business_day(base_date + timedelta(days=lag_days))
    return datetime(
        candidate.year,
        candidate.month,
        candidate.day,
        _MARKET_OPEN_UTC_HOUR,
        _MARKET_OPEN_UTC_MINUTE,
        tzinfo=UTC,
    )
