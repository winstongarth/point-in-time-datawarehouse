"""The seven signals, and the market cap two of them divide by.

Every signal is oriented so that **higher is better**, which is why
`low_accruals` carries a leading minus sign: high accruals are the bad end.
Orientation is not cosmetic - the scoring stage averages z-scores across a
group, so a single inverted signal would quietly cancel its partner out.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from pdw.quarterly import (
    QuarterFact,
    same_quarter_last_year,
    trailing_twelve_months,
    ttm_value,
)

SIGNAL_NAMES: tuple[str, ...] = (
    "earnings_yield",
    "cash_flow_yield",
    "revenue_growth",
    "earnings_surprise",
    "return_on_assets",
    "low_accruals",
    "momentum_12_1",
)

# Momentum skips the most recent month because one-month returns tend to
# *reverse* (Jegadeesh & Titman 1993); including it fights the 12-month
# trend it is supposed to measure.
MOMENTUM_LOOKBACK_SESSIONS = 252
MOMENTUM_SKIP_SESSIONS = 21

# How far back a cash-flow TTM may be sourced from when the latest quarter
# has an income statement but no usable cash-flow chain yet. Beyond this the
# reading is too stale to be worth the coverage.
MAX_CASH_FLOW_LAG_QUARTERS = 2


@dataclass(frozen=True)
class TickerFundamentals:
    """One ticker's rebuilt quarterly series as of a single signal date."""

    ticker: str
    net_income: list[QuarterFact]
    revenue: list[QuarterFact]
    operating_cash_flow: list[QuarterFact]
    shares_diluted: list[QuarterFact]
    total_assets: dict[date, float]  # instant facts, keyed by period_end
    latest_period_end: date | None


@dataclass(frozen=True)
class MarketCap:
    value: float
    shares: float
    price: float
    split_factor: float
    shares_period_end: date


def split_adjustment_factor(
    raw_close: float | None, split_adjusted_close: float | None
) -> float | None:
    """Cumulative split factor between a day and today, from two vendors.

    Tiingo's `close` is the raw historical quote; yfinance's is always
    retroactively split-adjusted by Yahoo's backend (see docs/limitations.md).
    Their ratio on the *same day* is therefore exactly the cumulative split
    factor applied since that day - 10.0 on any date before NVDA's June 2024
    10:1 split, 1.0 on any date after it.
    """
    if raw_close is None or split_adjusted_close is None:
        return None
    if split_adjusted_close <= 0 or raw_close <= 0:
        return None
    return raw_close / split_adjusted_close


def market_cap(
    shares: float,
    shares_period_end: date,
    price_on_signal_date: float,
    factor_at_period_end: float | None,
    factor_at_signal_date: float | None,
) -> MarketCap | None:
    """Share count rebased onto the signal date's split basis, times price.

    A filing's share count is in the units of its own period end. If the
    company split in between, multiplying it by today's price is off by the
    split ratio - so the count is rebased by the split factor accumulated
    *between* the two dates, which is the ratio of each date's factor to
    today.
    """
    if shares <= 0 or price_on_signal_date <= 0:
        return None
    if factor_at_period_end is None or factor_at_signal_date is None:
        return None
    if factor_at_signal_date <= 0:
        return None

    rebase = factor_at_period_end / factor_at_signal_date
    rebased_shares = shares * rebase
    value = rebased_shares * price_on_signal_date
    if value <= 0:
        return None
    return MarketCap(
        value=value,
        shares=rebased_shares,
        price=price_on_signal_date,
        split_factor=rebase,
        shares_period_end=shares_period_end,
    )


def earnings_yield(ttm_net_income: float | None, cap: MarketCap | None) -> float | None:
    if ttm_net_income is None or cap is None:
        return None
    return ttm_net_income / cap.value


def cash_flow_yield(ttm_operating_cash_flow: float | None, cap: MarketCap | None) -> float | None:
    if ttm_operating_cash_flow is None or cap is None:
        return None
    return ttm_operating_cash_flow / cap.value


def revenue_growth(quarters: list[QuarterFact], as_of_period_end: date) -> float | None:
    """Latest quarter's revenue over the seasonally-matched quarter a year ago."""
    current = next((q for q in quarters if q.period_end == as_of_period_end), None)
    if current is None:
        return None
    prior = same_quarter_last_year(quarters, current)
    if prior is None or prior.value <= 0:
        return None
    return current.value / prior.value - 1.0


def earnings_surprise(
    quarters: list[QuarterFact],
    as_of_period_end: date,
    min_history: int = 4,
    max_history: int = 8,
) -> float | None:
    """Standardised unexpected earnings: this quarter's YoY change in net
    income, divided by the standard deviation of that change over the prior
    few years.

    Post-earnings-announcement drift (Ball & Brown 1968; Bernard & Thomas
    1989) is the best-documented earnings effect there is: prices under-react
    to earnings news and keep drifting for weeks afterwards. Scaling by the
    firm's own volatility of surprises is what makes a $1bn beat at a steady
    utility count for more than a $1bn beat at BRK.B.
    """
    changes = _yoy_changes(quarters, as_of_period_end, max_history + 1)
    if changes is None or len(changes) < min_history + 1:
        return None
    current, history = changes[0], changes[1:]
    spread = statistics.pstdev(history)
    if spread <= 0:
        return None
    return current / spread


def _yoy_changes(
    quarters: list[QuarterFact], as_of_period_end: date, limit: int
) -> list[float] | None:
    """YoY net-income changes, most recent first, walking back quarter by quarter."""
    ordered = sorted(quarters, key=lambda q: q.period_end, reverse=True)
    start = next((i for i, q in enumerate(ordered) if q.period_end == as_of_period_end), None)
    if start is None:
        return None

    changes: list[float] = []
    for quarter in ordered[start:]:
        if len(changes) >= limit:
            break
        prior = same_quarter_last_year(quarters, quarter)
        if prior is None:
            break
        changes.append(quarter.value - prior.value)
    return changes


def return_on_assets(ttm_net_income: float | None, total_assets: float | None) -> float | None:
    """TTM net income over total assets.

    Assets rather than equity because several names here (MCD, HD) carry
    negative book equity after years of buybacks, which makes ROE either
    meaningless or spectacularly, misleadingly positive.
    """
    if ttm_net_income is None or total_assets is None or total_assets <= 0:
        return None
    return ttm_net_income / total_assets


def low_accruals(
    ttm_net_income: float | None,
    ttm_operating_cash_flow: float | None,
    total_assets: float | None,
) -> float | None:
    """Negated accruals: earnings not backed by cash tend to reverse (Sloan 1996)."""
    if ttm_net_income is None or ttm_operating_cash_flow is None:
        return None
    if total_assets is None or total_assets <= 0:
        return None
    return -(ttm_net_income - ttm_operating_cash_flow) / total_assets


def momentum_12_1(prices: Sequence[float]) -> float | None:
    """Return from ~12 months ago to ~1 month ago.

    `prices` is a total-return series ending on the signal date, oldest
    first.
    """
    needed = MOMENTUM_LOOKBACK_SESSIONS + 1
    if len(prices) < needed:
        return None
    start = prices[-needed]
    end = prices[-(MOMENTUM_SKIP_SESSIONS + 1)]
    if start <= 0 or end <= 0:
        return None
    return end / start - 1.0


def compute_signals(
    fundamentals: TickerFundamentals,
    cap: MarketCap | None,
    price_history: Sequence[float],
    signal_date: date,
    max_days_since_period_end: int,
    surprise_min_history: int = 4,
    surprise_max_history: int = 8,
) -> dict[str, float]:
    """Every signal computable for one ticker on one date.

    Signals that can't be computed are absent from the result rather than
    zero-filled: a missing signal must not read as an average one.
    """
    out: dict[str, float] = {}

    # Momentum needs no fundamentals at all, so it survives a stale filer.
    momentum = momentum_12_1(price_history)
    if momentum is not None:
        out["momentum_12_1"] = momentum

    period_end = fundamentals.latest_period_end
    if period_end is None:
        return out
    if (signal_date - period_end).days > max_days_since_period_end:
        # A filer this far behind has nothing current to say. Dropping the
        # fundamentals rather than the ticker keeps its momentum signal.
        return out

    ttm_ni = ttm_value(fundamentals.net_income, period_end)
    ttm_ocf = _ttm_at_or_before(fundamentals.operating_cash_flow, period_end)
    assets = _latest_instant(fundamentals.total_assets, period_end)

    for name, value in (
        ("earnings_yield", earnings_yield(ttm_ni, cap)),
        ("cash_flow_yield", cash_flow_yield(ttm_ocf, cap)),
        ("revenue_growth", revenue_growth(fundamentals.revenue, period_end)),
        (
            "earnings_surprise",
            earnings_surprise(
                fundamentals.net_income, period_end, surprise_min_history, surprise_max_history
            ),
        ),
        ("return_on_assets", return_on_assets(ttm_ni, assets)),
        ("low_accruals", low_accruals(ttm_ni, ttm_ocf, assets)),
    ):
        if value is not None:
            out[name] = value
    return out


def _ttm_at_or_before(quarters: list[QuarterFact], period_end: date) -> float | None:
    """TTM ending at `period_end`, else at the most recent earlier quarter.

    Cash flow lags net income for some filers - a 10-Q can carry the income
    statement for a quarter whose cash-flow statement is only reachable
    cumulatively later. Walking back a quarter or two keeps the signal rather
    than dropping the name, at the cost of a slightly staler reading.
    """
    candidates = sorted(
        {q.period_end for q in quarters if q.period_end <= period_end}, reverse=True
    )
    for candidate in candidates[:MAX_CASH_FLOW_LAG_QUARTERS]:
        value = ttm_value(quarters, candidate)
        if value is not None:
            return value
    return None


def _latest_instant(by_period_end: dict[date, float], on_or_before: date) -> float | None:
    eligible = [pe for pe in by_period_end if pe <= on_or_before]
    if not eligible:
        return None
    return by_period_end[max(eligible)]


def ttm_chain(quarters: list[QuarterFact], period_end: date) -> list[QuarterFact] | None:
    """Exposed for the report's traceability: which quarters a TTM summed."""
    return trailing_twelve_months(quarters, period_end)
