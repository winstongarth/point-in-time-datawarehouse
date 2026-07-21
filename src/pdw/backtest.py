from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import psycopg

from pdw.query import PointInTimeReader

# Naive quarterly-rebalanced earnings-yield long/short over the 50-name
# universe. Earnings yield = trailing-twelve-month net
# income / market cap (the reciprocal of trailing P/E) - a standard, simple
# value factor. Deliberately crude throughout: this backtest
# exists only to measure the point-in-time-vs-restated performance gap, not
# to be a good trading strategy.
QUANTILE_SIZE = 10  # top/bottom 10 of 50 -> long/short deciles-of-the-universe
MIN_VALID_TICKERS = 20  # need at least 2x QUANTILE_SIZE with computable data
TTM_QUARTERS = 4
PRICE_SEARCH_WINDOW_DAYS = 10  # nominal rebalance date is often a holiday/weekend

# A single-quarter duration fact, not a YTD-cumulative or annual one sharing
# the same fiscal_period label (the exact EDGAR shape behind M4's Verizon
# bug and M6's revenue_sanity bug) - reused here for the same reason.
_SINGLE_QUARTER_MIN_DAYS = 80
_SINGLE_QUARTER_MAX_DAYS = 100

# Market cap needs a price on the *same basis* as the reported share count:
# yfinance's Close/Adj Close are always split-adjusted to today's share
# count by Yahoo's own backend - multiplying
# that by a historical quarter's actual (pre-split) share count would
# silently understate market cap for any ticker that later split within the
# window. Tiingo's raw close doesn't have this problem, so it's used for
# market cap specifically. Returns still use yfinance adj_close, the correct
# field for a total-return series.
MARKET_CAP_PRICE_SOURCE = "tiingo"
RETURN_PRICE_SOURCE = "yfinance"


@dataclass(frozen=True)
class FactRef:
    fact_id: int
    accession_no: str | None
    period_end: date
    value: float


@dataclass(frozen=True)
class EarningsYieldInput:
    ticker: str
    earnings_yield: float
    ttm_net_income: float
    net_income_facts: tuple[FactRef, ...]  # the TTM_QUARTERS facts summed
    shares_fact: FactRef
    price_date: date
    price: float
    market_cap: float


@dataclass(frozen=True)
class Portfolio:
    rebalance_date: date
    long: tuple[str, ...]
    short: tuple[str, ...]
    candidates: dict[str, EarningsYieldInput]  # every ticker with computable data, for tracing


@dataclass(frozen=True)
class PeriodReturn:
    start_date: date
    end_date: date
    long_return: float
    short_return: float

    @property
    def portfolio_return(self) -> float:
        return self.long_return - self.short_return


@dataclass
class BacktestRun:
    mode: str  # "point_in_time" | "latest"
    portfolios: list[Portfolio] = field(default_factory=list)
    period_returns: list[PeriodReturn] = field(default_factory=list)


def generate_rebalance_dates(start: date, end: date) -> list[date]:
    """Quarterly calendar dates (Jan/Apr/Jul/Oct 1) from start to end inclusive."""
    month = start.month - ((start.month - 1) % 3)
    current = date(start.year, month, 1)
    if current < start:
        current = _next_quarter_start(current)
    dates = []
    while current <= end:
        dates.append(current)
        current = _next_quarter_start(current)
    return dates


def _next_quarter_start(d: date) -> date:
    month = d.month + 3
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    return date(year, month, 1)


def _nearest_price(
    conn: psycopg.Connection,
    as_of: datetime,
    ticker: str,
    target: date,
    *,
    source: str,
    field_name: str,
) -> tuple[date, float] | None:
    """The first available trading day on or after `target`, within a short
    search window - `target` is a nominal calendar date (often a holiday or
    weekend), not necessarily a trading day."""
    reader = PointInTimeReader(conn, as_of)
    df = reader.prices(
        [ticker], target, target + timedelta(days=PRICE_SEARCH_WINDOW_DAYS), source=source
    )
    if df.height == 0:
        return None
    df = df.sort("trade_date")
    row = df.row(0, named=True)
    value = row[field_name]
    if value is None:
        return None
    return (row["trade_date"], float(value))


def _ttm_net_income(
    conn: psycopg.Connection, as_of: datetime, ticker: str, rebalance_date: date
) -> tuple[float, tuple[FactRef, ...]] | None:
    """The 4 quarters ending at or before `rebalance_date` - *not* simply
    "whatever's visible as of `as_of`". Those coincide for point_in_time
    (a fact visible at all implies its period_end already precedes the
    rebalance, since filing always follows the period it describes), but
    for mode="latest" (as_of=now for every historical rebalance) they
    don't: without this filter, every rebalance would compare against
    today's most recent quarters regardless of which historical date was
    being evaluated - found live, when a full run showed ~90% of every
    rebalance's positions "differing" (comparing entirely different
    calendar periods, not measuring restatement's effect on the same
    periods)."""
    reader = PointInTimeReader(conn, as_of)
    df = reader.fundamentals(["net_income"], [ticker])
    if df.height == 0:
        return None
    df = df.filter(
        df["period_start"].is_not_null()
        & ((df["period_end"] - df["period_start"]).dt.total_days() >= _SINGLE_QUARTER_MIN_DAYS)
        & ((df["period_end"] - df["period_start"]).dt.total_days() <= _SINGLE_QUARTER_MAX_DAYS)
        & (df["period_end"] <= rebalance_date)
    ).sort("period_end")
    if df.height < TTM_QUARTERS:
        return None
    trailing = df.tail(TTM_QUARTERS)
    facts = tuple(
        FactRef(
            fact_id=row["fact_id"],
            accession_no=row["accession_no"],
            period_end=row["period_end"],
            value=row["value"],
        )
        for row in trailing.iter_rows(named=True)
    )
    return (sum(f.value for f in facts), facts)


def _latest_shares_outstanding(
    conn: psycopg.Connection, as_of: datetime, ticker: str, rebalance_date: date
) -> FactRef | None:
    """The most recent shares-outstanding fact as of `rebalance_date` -
    same "which period, not just which as_of" reasoning as `_ttm_net_income`."""
    reader = PointInTimeReader(conn, as_of)
    df = reader.fundamentals(["shares_outstanding_diluted"], [ticker])
    if df.height == 0:
        return None
    df = df.filter(df["period_end"] <= rebalance_date).sort("period_end")
    if df.height == 0:
        return None
    row = df.row(-1, named=True)
    return FactRef(
        fact_id=row["fact_id"],
        accession_no=row["accession_no"],
        period_end=row["period_end"],
        value=row["value"],
    )


def compute_earnings_yields(
    conn: psycopg.Connection,
    fundamentals_as_of: datetime,
    price_as_of: datetime,
    tickers: list[str],
    rebalance_date: date,
) -> dict[str, EarningsYieldInput]:
    """Every ticker with enough data to compute an earnings yield for
    `rebalance_date`. Tickers missing any input (TTM net income, shares
    outstanding, or a nearby price) are simply excluded - a documented
    simplification, not an error, consistent with this being a deliberately
    crude instrument.

    Two separate `as_of`s, not one: `fundamentals_as_of` is the actual axis
    this milestone measures (rebalance date for point-in-time, now for
    latest) and must stay exact. `price_as_of` is always "now" regardless
    of mode - prices aren't restated in the amended-filing sense this
    backtest is about, and using the rebalance date here instead would make
    every price permanently unknowable: a source's own availability lag
    means a trade_date's close only becomes knowable *after* that date,
    so a same-day as_of can never see any price for or after that date -
    found live, when a full run produced zero rebalances at all.
    """
    results: dict[str, EarningsYieldInput] = {}
    for ticker in tickers:
        ttm = _ttm_net_income(conn, fundamentals_as_of, ticker, rebalance_date)
        if ttm is None:
            continue
        ttm_net_income, net_income_facts = ttm
        shares = _latest_shares_outstanding(conn, fundamentals_as_of, ticker, rebalance_date)
        if shares is None or shares.value <= 0:
            continue
        price_point = _nearest_price(
            conn,
            price_as_of,
            ticker,
            rebalance_date,
            source=MARKET_CAP_PRICE_SOURCE,
            field_name="close",
        )
        if price_point is None:
            continue
        price_date, price = price_point
        market_cap = shares.value * price
        if market_cap <= 0:
            continue
        results[ticker] = EarningsYieldInput(
            ticker=ticker,
            earnings_yield=ttm_net_income / market_cap,
            ttm_net_income=ttm_net_income,
            net_income_facts=net_income_facts,
            shares_fact=shares,
            price_date=price_date,
            price=price,
            market_cap=market_cap,
        )
    return results


def build_portfolio(
    rebalance_date: date, candidates: dict[str, EarningsYieldInput]
) -> Portfolio | None:
    if len(candidates) < MIN_VALID_TICKERS:
        return None
    ranked = sorted(candidates.values(), key=lambda c: c.earnings_yield, reverse=True)
    long = tuple(c.ticker for c in ranked[:QUANTILE_SIZE])
    short = tuple(c.ticker for c in ranked[-QUANTILE_SIZE:])
    return Portfolio(rebalance_date=rebalance_date, long=long, short=short, candidates=candidates)


def compute_forward_return(
    conn: psycopg.Connection, as_of: datetime, ticker: str, start_date: date, end_date: date
) -> float | None:
    start_point = _nearest_price(
        conn, as_of, ticker, start_date, source=RETURN_PRICE_SOURCE, field_name="adj_close"
    )
    end_point = _nearest_price(
        conn, as_of, ticker, end_date, source=RETURN_PRICE_SOURCE, field_name="adj_close"
    )
    if start_point is None or end_point is None or start_point[1] == 0:
        return None
    return end_point[1] / start_point[1] - 1


def run_backtest(
    conn: psycopg.Connection,
    tickers: list[str],
    rebalance_dates: list[date],
    mode: str,
) -> BacktestRun:
    """Run the full quarterly-rebalanced long/short once, entirely through
    `PointInTimeReader` (the only sanctioned way to read
    core). mode="point_in_time" uses each rebalance's own historical date
    as the *fundamentals* `as_of`, so restated facts filed later are
    correctly invisible. mode="latest" uses today's fundamentals for every
    rebalance instead, so every fact reflects its final, fully-restated
    value - the contrast this milestone measures. Price/return lookups
    always use "now" in both modes (see compute_earnings_yields)."""
    if mode not in ("point_in_time", "latest"):
        raise ValueError(f"mode must be 'point_in_time' or 'latest', got {mode!r}")

    price_as_of = datetime.now(UTC)  # always "now" - see compute_earnings_yields's docstring
    run = BacktestRun(mode=mode)
    for rebalance_date in rebalance_dates:
        fundamentals_as_of = (
            datetime.combine(rebalance_date, datetime.min.time(), tzinfo=UTC)
            if mode == "point_in_time"
            else price_as_of
        )
        candidates = compute_earnings_yields(
            conn, fundamentals_as_of, price_as_of, tickers, rebalance_date
        )
        portfolio = build_portfolio(rebalance_date, candidates)
        if portfolio is not None:
            run.portfolios.append(portfolio)

    for current, nxt in zip(run.portfolios, run.portfolios[1:], strict=False):
        start, end = current.rebalance_date, nxt.rebalance_date
        long_returns = [
            r
            for t in current.long
            if (r := compute_forward_return(conn, price_as_of, t, start, end)) is not None
        ]
        short_returns = [
            r
            for t in current.short
            if (r := compute_forward_return(conn, price_as_of, t, start, end)) is not None
        ]
        if not long_returns or not short_returns:
            continue
        run.period_returns.append(
            PeriodReturn(
                start_date=current.rebalance_date,
                end_date=nxt.rebalance_date,
                long_return=statistics.fmean(long_returns),
                short_return=statistics.fmean(short_returns),
            )
        )
    return run


@dataclass(frozen=True)
class BacktestSummary:
    cumulative_return: float
    sharpe: float | None
    avg_turnover: float
    n_periods: int


def summarize(run: BacktestRun) -> BacktestSummary:
    returns = [p.portfolio_return for p in run.period_returns]
    cumulative = 1.0
    for r in returns:
        cumulative *= 1 + r
    cumulative_return = cumulative - 1
    sharpe = None
    if len(returns) >= 2 and statistics.pstdev(returns) > 0:
        sharpe = (statistics.fmean(returns) / statistics.stdev(returns)) * (4**0.5)
    turnovers = [
        _turnover(a, b) for a, b in zip(run.portfolios, run.portfolios[1:], strict=False)
    ]
    avg_turnover = statistics.fmean(turnovers) if turnovers else 0.0
    return BacktestSummary(
        cumulative_return=cumulative_return,
        sharpe=sharpe,
        avg_turnover=avg_turnover,
        n_periods=len(returns),
    )


def _turnover(previous: Portfolio, current: Portfolio) -> float:
    prev_slots = {(t, "long") for t in previous.long} | {(t, "short") for t in previous.short}
    cur_slots = {(t, "long") for t in current.long} | {(t, "short") for t in current.short}
    changed = len(prev_slots - cur_slots)
    return changed / len(cur_slots) if cur_slots else 0.0


def equity_curve(run: BacktestRun) -> list[tuple[date, float]]:
    curve = [(run.portfolios[0].rebalance_date, 1.0)] if run.portfolios else []
    cumulative = 1.0
    for p in run.period_returns:
        cumulative *= 1 + p.portfolio_return
        curve.append((p.end_date, cumulative))
    return curve


@dataclass(frozen=True)
class PositionDifference:
    rebalance_date: date
    ticker: str
    point_in_time_side: str | None  # "long" | "short" | None
    latest_side: str | None


def compare_portfolios(
    pit_run: BacktestRun, latest_run: BacktestRun
) -> list[PositionDifference]:
    """Positions that differ at the *same* rebalance date between the two
    runs - the measured effect of restatement alone, since both runs share
    the same rebalance schedule and forward-return windows."""
    latest_by_date = {p.rebalance_date: p for p in latest_run.portfolios}
    differences: list[PositionDifference] = []
    for pit_portfolio in pit_run.portfolios:
        latest_portfolio = latest_by_date.get(pit_portfolio.rebalance_date)
        if latest_portfolio is None:
            continue
        pit_sides = _side_map(pit_portfolio)
        latest_sides = _side_map(latest_portfolio)
        for ticker in sorted(set(pit_sides) | set(latest_sides)):
            pit_side = pit_sides.get(ticker)
            latest_side = latest_sides.get(ticker)
            if pit_side != latest_side:
                differences.append(
                    PositionDifference(
                        rebalance_date=pit_portfolio.rebalance_date,
                        ticker=ticker,
                        point_in_time_side=pit_side,
                        latest_side=latest_side,
                    )
                )
    return differences


def _side_map(portfolio: Portfolio) -> dict[str, str]:
    sides: dict[str, str] = {t: "long" for t in portfolio.long}
    sides.update({t: "short" for t in portfolio.short})
    return sides


@dataclass(frozen=True)
class CaseStudy:
    ticker: str
    rebalance_date: date
    period_end: date
    point_in_time_fact_id: int
    point_in_time_accession_no: str | None
    point_in_time_value: float
    latest_fact_id: int
    latest_accession_no: str | None
    latest_value: float
    point_in_time_side: str | None
    latest_side: str | None


_SVG_WIDTH = 720
_SVG_HEIGHT = 360
_SVG_MARGIN = 48


def render_equity_curve_svg(
    pit_curve: list[tuple[date, float]], latest_curve: list[tuple[date, float]]
) -> str:
    """Hand-written SVG, no charting dependency added - a plain two-line
    chart is well within what's reasonable to write by hand, same spirit as
    this project's hand-written SQL."""
    all_points = pit_curve + latest_curve
    if not all_points:
        return "<svg></svg>"
    dates = [d for d, _ in all_points]
    values = [v for _, v in all_points]
    min_date, max_date = min(dates), max(dates)
    min_val, max_val = min(values + [1.0]), max(values + [1.0])
    date_span = max((max_date - min_date).days, 1)
    val_span = max(max_val - min_val, 1e-9)

    plot_w = _SVG_WIDTH - 2 * _SVG_MARGIN
    plot_h = _SVG_HEIGHT - 2 * _SVG_MARGIN

    def _points(curve: list[tuple[date, float]]) -> str:
        coords = []
        for d, v in curve:
            x = _SVG_MARGIN + (d - min_date).days / date_span * plot_w
            y = _SVG_MARGIN + (1 - (v - min_val) / val_span) * plot_h
            coords.append(f"{x:.1f},{y:.1f}")
        return " ".join(coords)

    baseline_y = _SVG_MARGIN + (1 - (1.0 - min_val) / val_span) * plot_h
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_SVG_WIDTH} {_SVG_HEIGHT}" \
font-family="sans-serif" font-size="12">
  <rect x="0" y="0" width="{_SVG_WIDTH}" height="{_SVG_HEIGHT}" fill="white" />
  <line x1="{_SVG_MARGIN}" y1="{baseline_y:.1f}" x2="{_SVG_WIDTH - _SVG_MARGIN}" \
y2="{baseline_y:.1f}" stroke="#ccc" stroke-dasharray="4,4" />
  <text x="{_SVG_MARGIN}" y="20">Equity curve: point-in-time vs latest-restated</text>
  <polyline points="{_points(pit_curve)}" fill="none" stroke="#1f77b4" stroke-width="2" />
  <polyline points="{_points(latest_curve)}" fill="none" stroke="#d62728" stroke-width="2" />
  <rect x="{_SVG_WIDTH - _SVG_MARGIN - 160}" y="{_SVG_MARGIN}" width="12" height="12" \
fill="#1f77b4" />
  <text x="{_SVG_WIDTH - _SVG_MARGIN - 142}" y="{_SVG_MARGIN + 10}">point_in_time</text>
  <rect x="{_SVG_WIDTH - _SVG_MARGIN - 160}" y="{_SVG_MARGIN + 18}" width="12" height="12" \
fill="#d62728" />
  <text x="{_SVG_WIDTH - _SVG_MARGIN - 142}" y="{_SVG_MARGIN + 28}">latest</text>
  <text x="{_SVG_MARGIN}" y="{_SVG_HEIGHT - 10}">{min_date.isoformat()}</text>
  <text x="{_SVG_WIDTH - _SVG_MARGIN - 70}" y="{_SVG_HEIGHT - 10}">{max_date.isoformat()}</text>
</svg>
"""


def render_findings_report(
    pit_run: BacktestRun,
    latest_run: BacktestRun,
    differences: list[PositionDifference],
    case_studies: list[CaseStudy],
    chart_path: str,
) -> str:
    pit_summary = summarize(pit_run)
    latest_summary = summarize(latest_run)
    rebalances_with_differences = len({d.rebalance_date for d in differences})

    lines = [
        "# Findings: point-in-time vs. latest-restated backtest",
        "",
        "Naive quarterly-rebalanced earnings-yield long/short over the 50-name universe, "
        "run twice through the same `PointInTimeReader`-backed pipeline: "
        "once with `as_of` fixed to each historical rebalance date, once with `.latest()` for "
        "every rebalance. Earnings yield = trailing-twelve-month net income / market cap "
        "(diluted weighted-average shares x price, nearest trading day at/after the rebalance "
        "date). Long the top 10 names, short the bottom 10, equal-weighted, held one quarter.",
        "",
        "**Methodology note on price fields:** market cap uses Tiingo's raw `close` (matches "
        "the true historical share count basis); forward returns use yfinance `adj_close` "
        "(the correct field for a total-return series). yfinance's own `close` is always "
        "split-adjusted by Yahoo's backend regardless of fetch flags "
        "and would silently understate market cap for any ticker that later split "
        "within the window.",
        "",
        "## Comparison",
        "",
        "| Metric | Point-in-time | Latest (restated) |",
        "|---|---|---|",
        f"| Cumulative return | {pit_summary.cumulative_return:.2%} "
        f"| {latest_summary.cumulative_return:.2%} |",
        f"| Sharpe (quarterly, annualized) | "
        f"{_fmt_sharpe(pit_summary.sharpe)} | {_fmt_sharpe(latest_summary.sharpe)} |",
        f"| Avg. turnover per rebalance | {pit_summary.avg_turnover:.1%} "
        f"| {latest_summary.avg_turnover:.1%} |",
        f"| Rebalance periods | {pit_summary.n_periods} | {latest_summary.n_periods} |",
        "",
        f"**{len(differences)} individual position differences** across "
        f"**{rebalances_with_differences} of {len(pit_run.portfolios)} rebalance dates** "
        "were caused purely by restatement - both runs share the same rebalance schedule and "
        "forward-return windows, so any difference in portfolio membership at a given date is "
        "attributable to a fundamentals fact having a different value in the point-in-time view "
        "than in today's fully-restated view.",
        "",
        f"![Equity curves]({chart_path})",
        "",
        "## Case studies",
        "",
        "Each row traces one restatement that changed portfolio membership at a specific "
        "rebalance, back to the exact `fact_id`s and accession numbers of both the original "
        "and restated values.",
        "",
    ]
    for i, cs in enumerate(case_studies, start=1):
        lines += [
            f"### {i}. {cs.ticker} — {cs.rebalance_date.isoformat()} rebalance",
            "",
            f"- Period: {cs.period_end.isoformat()} net income",
            f"- Point-in-time: **{cs.point_in_time_value:,.0f}** "
            f"(`fact_id={cs.point_in_time_fact_id}`, accession `{cs.point_in_time_accession_no}`) "
            f"-> {cs.point_in_time_side or 'excluded'}",
            f"- Latest (restated): **{cs.latest_value:,.0f}** "
            f"(`fact_id={cs.latest_fact_id}`, accession `{cs.latest_accession_no}`) "
            f"-> {cs.latest_side or 'excluded'}",
            "",
        ]
    return "\n".join(lines)


def _fmt_sharpe(sharpe: float | None) -> str:
    return f"{sharpe:.2f}" if sharpe is not None else "n/a"


def find_case_studies(
    differences: list[PositionDifference], pit_run: BacktestRun, latest_run: BacktestRun
) -> list[CaseStudy]:
    """For each differing position, trace it back to the specific
    net_income fact(s) that changed between the point-in-time and latest
    views - each case study is linked to a specific fact_id and accession
    number."""
    pit_by_date = {p.rebalance_date: p for p in pit_run.portfolios}
    latest_by_date = {p.rebalance_date: p for p in latest_run.portfolios}
    case_studies: list[CaseStudy] = []
    for diff in differences:
        pit_portfolio = pit_by_date.get(diff.rebalance_date)
        latest_portfolio = latest_by_date.get(diff.rebalance_date)
        if pit_portfolio is None or latest_portfolio is None:
            continue
        pit_input = pit_portfolio.candidates.get(diff.ticker)
        latest_input = latest_portfolio.candidates.get(diff.ticker)
        if pit_input is None or latest_input is None:
            continue
        latest_facts_by_period = {f.period_end: f for f in latest_input.net_income_facts}
        for pit_fact in pit_input.net_income_facts:
            latest_fact = latest_facts_by_period.get(pit_fact.period_end)
            if latest_fact is not None and latest_fact.value != pit_fact.value:
                case_studies.append(
                    CaseStudy(
                        ticker=diff.ticker,
                        rebalance_date=diff.rebalance_date,
                        period_end=pit_fact.period_end,
                        point_in_time_fact_id=pit_fact.fact_id,
                        point_in_time_accession_no=pit_fact.accession_no,
                        point_in_time_value=pit_fact.value,
                        latest_fact_id=latest_fact.fact_id,
                        latest_accession_no=latest_fact.accession_no,
                        latest_value=latest_fact.value,
                        point_in_time_side=diff.point_in_time_side,
                        latest_side=diff.latest_side,
                    )
                )
    return case_studies
