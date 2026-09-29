"""Rebuilding true fiscal quarters out of what EDGAR actually tags.

EDGAR almost never tags Q4 on its own - a 10-K reports the fiscal *year* -
and cash-flow statements are year-to-date in every 10-Q. So the set of facts
carrying a standalone Q4 is close to empty, and a naive "take the last four
3-month facts" both skips Q4 entirely and silently sums four quarters
spanning ~15 months.

This module derives the missing quarters by differencing cumulative facts
that share a fiscal-year start: Q4 = FY - 9M, and a Q2 cash flow = 6M - 3M.
A directly-reported 3-month fact always wins over a derived one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Day-count bands for the cumulative shapes EDGAR actually files, set from
# the observed distribution of `period_end - period_start` across the whole
# universe rather than from calendar arithmetic. The clusters are wide and
# well separated (83-111, 167-188, 251-279, 363-370) because retailers file
# 52/53-week fiscal years: COST's quarters are 12 weeks with a 16-week Q4, so
# a band of 80-100 days silently drops both its Q4 *and* its half-year and
# nine-month cumulatives - which is how its cash-flow series ends up with a
# third of the quarters it should have.
_SHAPE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("Q1", 80, 120),
    ("H1", 160, 200),
    ("M9", 245, 290),
    ("FY", 350, 385),
)

_SHAPE_ORDER = {"Q1": 1, "H1": 2, "M9": 3, "FY": 4}

# Two facts belong to the same fiscal year if their period_start is within
# this many days - filers shift the fiscal-year start by a few days a year.
_SAME_START_TOLERANCE_DAYS = 10

# Consecutive quarters: this quarter's start should land within a few days of
# the previous quarter's end.
_CONSECUTIVE_GAP_TOLERANCE_DAYS = 10


@dataclass(frozen=True)
class RawDurationFact:
    """A duration fact as it comes out of `PointInTimeReader.fundamentals`."""

    period_start: date
    period_end: date
    value: float
    filed_date: date
    fact_id: int
    accession_no: str | None

    @property
    def length_days(self) -> int:
        return (self.period_end - self.period_start).days


@dataclass(frozen=True)
class QuarterFact:
    """One fiscal quarter's worth of a flow metric."""

    period_start: date
    period_end: date
    value: float
    filed_date: date
    fact_id: int
    accession_no: str | None
    derived: bool  # True when differenced out of two cumulative facts

    @property
    def length_days(self) -> int:
        return (self.period_end - self.period_start).days


def classify_shape(length_days: int) -> str | None:
    """Name the cumulative shape a duration covers, or None if unrecognised."""
    for name, low, high in _SHAPE_BANDS:
        if low <= length_days <= high:
            return name
    return None


def quarterly_series(facts: list[RawDurationFact]) -> list[QuarterFact]:
    """Every fiscal quarter recoverable from `facts`, oldest first.

    A directly-reported 3-month fact is kept as-is. Otherwise the quarter is
    derived from the two cumulative facts that bracket it - the one ending at
    this quarter's end, minus the next-shorter one sharing the same
    fiscal-year start. Quarters recoverable neither way are simply absent;
    callers must not assume a contiguous series.
    """
    if not facts:
        return []

    direct: dict[date, QuarterFact] = {}
    cumulative: list[tuple[str, RawDurationFact]] = []
    for fact in facts:
        shape = classify_shape(fact.length_days)
        if shape is None:
            continue
        # A 3-month fact is *both* a reported quarter and, when it starts the
        # fiscal year, the shortest cumulative - so it belongs in both lists.
        # Filing it only as "direct" is what breaks Q2 = H1 - Q1: the
        # half-year fact then has no predecessor to difference against, and
        # every filer whose cash-flow statement is year-to-date loses every
        # quarter after its first.
        cumulative.append((shape, fact))
        if shape != "Q1":
            continue
        # Two filings can report the same quarter (an original and an
        # amendment). The reader already applied the knowledge-time filter,
        # so at most one should be current; if both somehow are, the later
        # filing wins.
        existing = direct.get(fact.period_end)
        if existing is None or fact.filed_date >= existing.filed_date:
            direct[fact.period_end] = QuarterFact(
                period_start=fact.period_start,
                period_end=fact.period_end,
                value=fact.value,
                filed_date=fact.filed_date,
                fact_id=fact.fact_id,
                accession_no=fact.accession_no,
                derived=False,
            )

    derived = _derive_from_cumulative(cumulative, already_known=set(direct))
    return sorted([*direct.values(), *derived], key=lambda q: q.period_end)


def _derive_from_cumulative(
    cumulative: list[tuple[str, RawDurationFact]], already_known: set[date]
) -> list[QuarterFact]:
    """Q_n = cum_n - cum_(n-1), for cumulative facts sharing a fiscal-year start."""
    out: list[QuarterFact] = []
    for shape, fact in cumulative:
        if shape == "Q1" or fact.period_end in already_known:
            continue
        prior = _preceding_cumulative(cumulative, fact, _SHAPE_ORDER[shape])
        if prior is None:
            continue
        out.append(
            QuarterFact(
                period_start=prior.period_end,
                period_end=fact.period_end,
                value=fact.value - prior.value,
                # The differenced quarter is only knowable once *both* facts
                # are; the reader filtered both to the same as_of, so the
                # later filing date is the binding one.
                filed_date=max(fact.filed_date, prior.filed_date),
                fact_id=fact.fact_id,
                accession_no=fact.accession_no,
                derived=True,
            )
        )
    return out


def _preceding_cumulative(
    cumulative: list[tuple[str, RawDurationFact]],
    fact: RawDurationFact,
    fact_order: int,
) -> RawDurationFact | None:
    """The next-shorter cumulative fact sharing `fact`'s fiscal-year start."""
    if fact_order <= 1:
        return None
    wanted = fact_order - 1
    for shape, candidate in cumulative:
        if _SHAPE_ORDER[shape] != wanted:
            continue
        if abs((candidate.period_start - fact.period_start).days) > _SAME_START_TOLERANCE_DAYS:
            continue
        if candidate.period_end >= fact.period_end:
            continue
        return candidate
    return None


def latest_quarter(quarters: list[QuarterFact], on_or_before: date) -> QuarterFact | None:
    """The most recent quarter whose period ended on or before `on_or_before`."""
    eligible = [q for q in quarters if q.period_end <= on_or_before]
    if not eligible:
        return None
    return max(eligible, key=lambda q: q.period_end)


def trailing_twelve_months(
    quarters: list[QuarterFact], as_of_period_end: date, n: int = 4
) -> list[QuarterFact] | None:
    """The `n` consecutive quarters ending at `as_of_period_end`, or None.

    Consecutive is checked, not assumed: a gap anywhere in the chain returns
    None rather than a sum spanning fifteen months. That is the specific trap
    the original earnings-yield backtest falls into.
    """
    by_end = {q.period_end: q for q in quarters}
    current = by_end.get(as_of_period_end)
    if current is None:
        return None

    chain = [current]
    while len(chain) < n:
        earliest = chain[-1]
        previous = _quarter_ending_near(quarters, earliest.period_start)
        if previous is None:
            return None
        chain.append(previous)
    return list(reversed(chain))


def _quarter_ending_near(quarters: list[QuarterFact], target_end: date) -> QuarterFact | None:
    """The quarter whose period_end sits within tolerance of `target_end`."""
    best: QuarterFact | None = None
    best_gap = _CONSECUTIVE_GAP_TOLERANCE_DAYS + 1
    for quarter in quarters:
        gap = abs((quarter.period_end - target_end).days)
        if gap <= _CONSECUTIVE_GAP_TOLERANCE_DAYS and gap < best_gap:
            best, best_gap = quarter, gap
    return best


def ttm_value(quarters: list[QuarterFact], as_of_period_end: date) -> float | None:
    chain = trailing_twelve_months(quarters, as_of_period_end)
    if chain is None:
        return None
    return sum(q.value for q in chain)


def same_quarter_last_year(quarters: list[QuarterFact], quarter: QuarterFact) -> QuarterFact | None:
    """The seasonally-matched quarter roughly 365 days earlier.

    Seasonal matching matters: a retailer earns most of its profit in one
    quarter, so comparing Q4 against Q3 measures the calendar, not the
    business.
    """
    target_end = quarter.period_end - timedelta(days=365)
    best: QuarterFact | None = None
    best_gap = 45
    for candidate in quarters:
        if candidate.period_end >= quarter.period_end:
            continue
        gap = abs((candidate.period_end - target_end).days)
        if gap < best_gap:
            best, best_gap = candidate, gap
    return best
