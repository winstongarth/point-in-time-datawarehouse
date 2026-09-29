"""The three fundamentals views the look-ahead decomposition compares.

`docs/findings.md`'s original experiment contrasts point-in-time against
"today's data", which conflates two different mistakes: using a *revised*
value for a quarter that had been reported, and using a quarter that had not
been filed at all. Separating them is the whole point of this module - they
turn out to matter by wildly different amounts.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

import polars as pl

# A filing's share count is stated in the units of its own period end. Later
# filings restate historical share counts onto a post-split basis, so letting
# a "restated" view see them would move market cap mechanically (NVDA's 10:1
# split in June 2024 divides every prior share count by ten) rather than
# economically. Share counts therefore stay point-in-time in every view.
ALWAYS_POINT_IN_TIME_METRICS = frozenset({"shares_outstanding_diluted"})

_FACT_KEY = ["ticker", "metric_code", "period_start", "period_end"]


class FundamentalsView(StrEnum):
    POINT_IN_TIME = "point_in_time"
    RESTATED_KNOWN_PERIODS = "restated_known_periods"
    LATEST_NAIVE = "latest_naive"

    @property
    def label(self) -> str:
        return {
            FundamentalsView.POINT_IN_TIME: "Point-in-time",
            FundamentalsView.RESTATED_KNOWN_PERIODS: "Restated values, known periods only",
            FundamentalsView.LATEST_NAIVE: "Latest data (naive)",
        }[self]

    @property
    def measures(self) -> str:
        return {
            FundamentalsView.POINT_IN_TIME: "-",
            FundamentalsView.RESTATED_KNOWN_PERIODS: "Restatement bias alone",
            FundamentalsView.LATEST_NAIVE: "Reporting-lag look-ahead",
        }[self]


def make_fundamentals_view(
    view: FundamentalsView,
    point_in_time: pl.DataFrame,
    latest: pl.DataFrame,
    signal_date: date,
) -> pl.DataFrame:
    """The fundamentals a strategy run under `view` is allowed to see.

    `point_in_time` is a read at the signal cutoff; `latest` is a read as of
    now. Both carry the same columns (see `PointInTimeReader.fundamentals`).
    """
    if view is FundamentalsView.POINT_IN_TIME:
        return point_in_time

    if view is FundamentalsView.RESTATED_KNOWN_PERIODS:
        # Same periods the point-in-time view could see - so nothing unfiled
        # leaks in - but carrying today's revised values for them.
        known = point_in_time.select(_FACT_KEY).unique()
        restated = latest.join(known, on=_FACT_KEY, how="semi")
    else:
        # Every quarter that had *ended* by the signal date, filed or not.
        # This is the view that trades a quarter weeks before its 10-Q exists.
        restated = latest.filter(pl.col("period_end") <= signal_date)

    return _restore_point_in_time_metrics(restated, point_in_time)


def _restore_point_in_time_metrics(
    restated: pl.DataFrame, point_in_time: pl.DataFrame
) -> pl.DataFrame:
    """Swap the always-point-in-time metrics back to their as-filed values."""
    metrics = list(ALWAYS_POINT_IN_TIME_METRICS)
    return pl.concat(
        [
            restated.filter(~pl.col("metric_code").is_in(metrics)),
            point_in_time.filter(pl.col("metric_code").is_in(metrics)),
        ],
        how="vertical",
    )
