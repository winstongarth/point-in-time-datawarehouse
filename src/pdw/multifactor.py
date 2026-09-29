"""The multi-factor earnings strategy: signals in, daily equity curve out.

Where `pdw backtest run` is a deliberately crude instrument for *measuring*
look-ahead bias, this is a strategy that could plausibly be traded - monthly
rebalanced, dollar-neutral, inverse-volatility sized, and paying realistic
costs. Both read `core` only through `PointInTimeReader`.

The one-session gap between signal and trade is the load-bearing detail: a
signal computed from today's close cannot also be traded at today's close,
and a backtest that pretends otherwise books a day of return it could never
have captured.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import psycopg
import yaml

from pdw.data import FundamentalsView, make_fundamentals_view
from pdw.quarterly import QuarterFact, RawDurationFact, latest_quarter, quarterly_series
from pdw.scoring import GroupSpec, ScoredDate, ScoringConfig, score_date, spearman
from pdw.signals import (
    SIGNAL_NAMES,
    MarketCap,
    TickerFundamentals,
    compute_signals,
    market_cap,
    split_adjustment_factor,
)

METRICS = [
    "revenue",
    "net_income",
    "operating_cash_flow",
    "total_assets",
    "shares_outstanding_diluted",
]

# Market cap needs a raw quote and a split-adjusted one on the same day to
# recover the split factor; returns need a total-return series. See
# docs/limitations.md on why yfinance's `close` is not a raw quote.
RAW_PRICE_SOURCE = "tiingo"
RETURN_PRICE_SOURCE = "yfinance"

# Everything after this marker in docs/findings.md belongs to this strategy;
# `pdw backtest run` regenerates only what comes before it.
SECTION_MARKER = "<!-- pdw:strategy-report -->"

_FLOW_METRICS = ("revenue", "net_income", "operating_cash_flow", "shares_outstanding_diluted")
_PRICE_MATCH_WINDOW_DAYS = 10


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Exclusion:
    tickers: frozenset[str]
    signals: frozenset[str]
    reason: str


@dataclass(frozen=True)
class MultifactorConfig:
    rebalance: str
    mode: str
    entry_rank: int
    hold_rank: int
    min_scored: int
    scoring: ScoringConfig
    vol_lookback_days: int
    max_weight: float
    execution_lag_sessions: int
    cost_bps_per_dollar: float
    borrow_bps_per_year: float
    trading_days_per_year: int
    max_days_since_period_end: int
    exclusions: tuple[Exclusion, ...]
    surprise_min_history: int
    surprise_max_history: int

    def excluded_signals(self, ticker: str) -> frozenset[str]:
        out: set[str] = set()
        for exclusion in self.exclusions:
            if ticker in exclusion.tickers:
                out |= exclusion.signals
        return frozenset(out)


def load_config(path: Path) -> MultifactorConfig:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    signals = raw["signals"]
    groups = tuple(
        GroupSpec(name=name, weight=float(spec["weight"]), signals=tuple(spec["signals"]))
        for name, spec in signals["groups"].items()
    )
    unknown = {s for g in groups for s in g.signals} - set(SIGNAL_NAMES)
    if unknown:
        raise ValueError(f"{path}: unknown signal(s) {sorted(unknown)}")

    scoring = raw["scoring"]
    sizing = raw["sizing"]
    frictions = raw["frictions"]
    surprise = raw.get("earnings_surprise", {})
    return MultifactorConfig(
        rebalance=raw["rebalance"],
        mode=raw["mode"],
        entry_rank=int(raw["universe_size"]["entry_rank"]),
        hold_rank=int(raw["universe_size"]["hold_rank"]),
        min_scored=int(raw["universe_size"]["min_scored"]),
        scoring=ScoringConfig(
            groups=groups,
            min_groups=int(signals["min_groups"]),
            winsorize_pct=float(scoring["winsorize_pct"]),
            zscore_clip=float(scoring["zscore_clip"]),
            min_tickers_per_signal=int(scoring["min_tickers_per_signal"]),
            sector_neutralize=bool(scoring["sector_neutralize"]),
            min_sector_size=int(scoring["min_sector_size"]),
        ),
        vol_lookback_days=int(sizing["vol_lookback_days"]),
        max_weight=float(sizing["max_weight"]),
        execution_lag_sessions=int(frictions["execution_lag_sessions"]),
        cost_bps_per_dollar=float(frictions["cost_bps_per_dollar"]),
        borrow_bps_per_year=float(frictions["borrow_bps_per_year"]),
        trading_days_per_year=int(frictions["trading_days_per_year"]),
        max_days_since_period_end=int(raw["staleness"]["max_days_since_period_end"]),
        exclusions=tuple(
            Exclusion(
                tickers=frozenset(item["tickers"]),
                signals=frozenset(item["signals"]),
                reason=str(item.get("reason", "")).strip(),
            )
            for item in raw.get("exclusions", [])
        ),
        surprise_min_history=int(surprise.get("min_history_quarters", 4)),
        surprise_max_history=int(surprise.get("max_history_quarters", 8)),
    )


def load_sectors(path: Path) -> dict[str, str]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in raw["sectors"].items()}


# --------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------


@dataclass
class PricePanel:
    """Every price series the strategy needs, loaded once.

    Prices are read with `.latest()`, not at each signal date's as_of: a
    source's availability lag means a session's close only becomes knowable
    *after* that session, so a same-day as_of can never see it. Prices are
    also not restated in the amended-filing sense this project is about -
    the look-ahead decomposition varies the fundamentals view alone.
    """

    sessions: list[date]
    adj_close: dict[str, dict[date, float]]
    raw_close: dict[str, dict[date, float]]
    split_close: dict[str, dict[date, float]]

    def session_index(self, day: date) -> int | None:
        try:
            return self.sessions.index(day)
        except ValueError:
            return None

    def returns(self, ticker: str, previous: date, current: date) -> float | None:
        series = self.adj_close.get(ticker, {})
        before, after = series.get(previous), series.get(current)
        if before is None or after is None or before <= 0:
            return None
        return after / before - 1.0

    def history(self, ticker: str, upto: date, length: int) -> list[float]:
        """The `length` most recent adjusted closes ending on or before `upto`."""
        series = self.adj_close.get(ticker, {})
        out = [series[d] for d in self.sessions if d <= upto and d in series]
        return out[-length:]

    def split_factor(self, ticker: str, day: date) -> float | None:
        """Cumulative split factor between `day` and today, or None."""
        raw = self._nearest(self.raw_close.get(ticker, {}), day)
        split = self._nearest(self.split_close.get(ticker, {}), day)
        return split_adjustment_factor(raw, split)

    def _nearest(self, series: dict[date, float], day: date) -> float | None:
        """Exact match, else the closest session within a short window.

        Share counts are stated as of a fiscal period end, which is routinely
        a weekend or a holiday.
        """
        if day in series:
            return series[day]
        for offset in range(1, _PRICE_MATCH_WINDOW_DAYS + 1):
            for candidate in (day - timedelta(days=offset), day + timedelta(days=offset)):
                if candidate in series:
                    return series[candidate]
        return None


def load_price_panel(
    conn: psycopg.Connection, tickers: list[str], start: date, end: date
) -> PricePanel:
    from pdw.query import PointInTimeReader

    reader = PointInTimeReader(conn, datetime.now(UTC))
    returns_df = reader.prices(tickers, start, end, source=RETURN_PRICE_SOURCE)
    raw_df = reader.prices(tickers, start, end, source=RAW_PRICE_SOURCE)

    adj_close = _pivot(returns_df, "adj_close")
    split_close = _pivot(returns_df, "close")
    raw_close = _pivot(raw_df, "close")
    sessions = sorted({d for series in adj_close.values() for d in series})
    return PricePanel(
        sessions=sessions,
        adj_close=adj_close,
        raw_close=raw_close,
        split_close=split_close,
    )


def _pivot(df: pl.DataFrame, column: str) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {}
    if df.is_empty():
        return out
    for ticker, trade_date, value in df.select("ticker", "trade_date", column).iter_rows():
        if value is None:
            continue
        out.setdefault(ticker, {})[trade_date] = float(value)
    return out


def rebalance_sessions(sessions: list[date], cadence: str, start: date, end: date) -> list[date]:
    """The last session of each month (or quarter) in range.

    Derived from sessions actually present in the data rather than from a
    calendar, so a month-end holiday resolves to the real last trading day.
    """
    if cadence not in ("monthly", "quarterly"):
        raise ValueError(f"rebalance must be 'monthly' or 'quarterly', got {cadence!r}")

    last_of_month: dict[tuple[int, int], date] = {}
    for session in sessions:
        if start <= session <= end:
            last_of_month[(session.year, session.month)] = session

    out = [last_of_month[key] for key in sorted(last_of_month)]
    if cadence == "quarterly":
        out = [d for d in out if d.month % 3 == 0]
    return out


# --------------------------------------------------------------------------
# Fundamentals
# --------------------------------------------------------------------------


def build_ticker_fundamentals(facts: pl.DataFrame, ticker: str) -> TickerFundamentals:
    """Rebuild one ticker's quarterly series from a fundamentals frame."""
    rows = facts.filter(pl.col("ticker") == ticker)
    series: dict[str, list[QuarterFact]] = {}
    for metric in _FLOW_METRICS:
        series[metric] = quarterly_series(_raw_duration_facts(rows, metric))

    assets: dict[date, float] = {}
    for period_end, value in (
        rows.filter(pl.col("metric_code") == "total_assets")
        .select("period_end", "value")
        .iter_rows()
    ):
        if value is not None:
            assets[period_end] = float(value)

    # The income statement is what defines "the latest reported quarter":
    # revenue and net income arrive together, while the cash-flow statement
    # can lag (see signals._ttm_at_or_before).
    newest = latest_quarter(series["net_income"], date.max)
    return TickerFundamentals(
        ticker=ticker,
        net_income=series["net_income"],
        revenue=series["revenue"],
        operating_cash_flow=series["operating_cash_flow"],
        shares_diluted=series["shares_outstanding_diluted"],
        total_assets=assets,
        latest_period_end=newest.period_end if newest else None,
    )


def _raw_duration_facts(rows: pl.DataFrame, metric: str) -> list[RawDurationFact]:
    selected = rows.filter(
        (pl.col("metric_code") == metric) & pl.col("period_start").is_not_null()
    ).select("period_start", "period_end", "value", "filed_date", "fact_id", "accession_no")
    out: list[RawDurationFact] = []
    for period_start, period_end, value, filed_date, fact_id, accession_no in selected.iter_rows():
        if value is None:
            continue
        out.append(
            RawDurationFact(
                period_start=period_start,
                period_end=period_end,
                value=float(value),
                filed_date=filed_date,
                fact_id=int(fact_id),
                accession_no=accession_no,
            )
        )
    return out


def compute_market_cap(
    fundamentals: TickerFundamentals, panel: PricePanel, signal_date: date
) -> MarketCap | None:
    if fundamentals.latest_period_end is None:
        return None
    shares = latest_quarter(fundamentals.shares_diluted, fundamentals.latest_period_end)
    if shares is None or shares.value <= 0:
        return None
    price = panel.raw_close.get(fundamentals.ticker, {}).get(signal_date)
    if price is None:
        return None
    return market_cap(
        shares=shares.value,
        shares_period_end=shares.period_end,
        price_on_signal_date=price,
        factor_at_period_end=panel.split_factor(fundamentals.ticker, shares.period_end),
        factor_at_signal_date=panel.split_factor(fundamentals.ticker, signal_date),
    )


# --------------------------------------------------------------------------
# Book construction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rebalance:
    signal_date: date
    trade_date: date
    long: tuple[str, ...]
    short: tuple[str, ...]
    weights: dict[str, float]  # signed target weight of NAV
    scored: ScoredDate
    signals: dict[str, dict[str, float]]  # ticker -> signal -> raw value
    turnover: float = 0.0


def select_book(
    composites: dict[str, float],
    previous: tuple[str, ...],
    entry_rank: int,
    hold_rank: int,
    *,
    short_side: bool,
) -> tuple[str, ...]:
    """Top (or bottom) `entry_rank` names, with a hysteresis buffer.

    A name enters only inside the top 10 but is held while it stays inside
    the top 15. Scores are noisy; without the buffer, names sitting around
    rank 10 flip in and out every month and the book pays to trade noise.
    """
    ranked = sorted(composites, key=lambda t: composites[t], reverse=not short_side)
    entrants = ranked[:entry_rank]
    holdable = set(ranked[:hold_rank])

    book = [t for t in previous if t in holdable]
    for ticker in entrants:
        if len(book) >= entry_rank:
            break
        if ticker not in book:
            book.append(ticker)
    return tuple(book[:entry_rank])


def inverse_volatility_weights(
    tickers: tuple[str, ...], panel: PricePanel, signal_date: date, lookback: int, cap: float
) -> dict[str, float]:
    """Weights proportional to 1/volatility, capped, summing to 1.

    Equal weighting would let TSLA carry several times KO's risk while
    claiming to be the same size.
    """
    vols: dict[str, float] = {}
    for ticker in tickers:
        vol = _realized_volatility(panel, ticker, signal_date, lookback)
        if vol is not None and vol > 0:
            vols[ticker] = vol
    if not vols:
        # No usable history: fall back to equal weight rather than dropping
        # names the scoring stage deliberately selected.
        return {t: 1.0 / len(tickers) for t in tickers} if tickers else {}

    median_vol = statistics.median(vols.values())
    raw = {t: 1.0 / vols.get(t, median_vol) for t in tickers}
    total = sum(raw.values())
    return _apply_cap({t: v / total for t, v in raw.items()}, cap)


def _realized_volatility(
    panel: PricePanel, ticker: str, signal_date: date, lookback: int
) -> float | None:
    closes = panel.history(ticker, signal_date, lookback + 1)
    if len(closes) < max(10, lookback // 4):
        return None
    rets = [
        closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0
    ]
    if len(rets) < 2:
        return None
    return statistics.pstdev(rets)


def _apply_cap(weights: dict[str, float], cap: float) -> dict[str, float]:
    """Water-fill: clip at the cap, spread the excess over the uncapped names."""
    if not weights or cap <= 0:
        return weights
    if cap * len(weights) <= 1.0:
        return {t: 1.0 / len(weights) for t in weights}

    out = dict(weights)
    for _ in range(len(weights)):
        excess = sum(w - cap for w in out.values() if w > cap)
        if excess <= 1e-12:
            break
        free = {t: w for t, w in out.items() if w < cap}
        free_total = sum(free.values())
        if free_total <= 0:
            break
        for ticker in out:
            if out[ticker] > cap:
                out[ticker] = cap
            elif ticker in free:
                out[ticker] += excess * free[ticker] / free_total
    total = sum(out.values())
    return {t: w / total for t, w in out.items()} if total > 0 else out


def target_weights(
    long: tuple[str, ...], short: tuple[str, ...], panel: PricePanel, signal_date: date,
    config: MultifactorConfig,
) -> dict[str, float]:
    """Signed target weights: +100% long and -100% short of NAV, or long-only."""
    weights: dict[str, float] = {}
    for ticker, weight in inverse_volatility_weights(
        long, panel, signal_date, config.vol_lookback_days, config.max_weight
    ).items():
        weights[ticker] = weight

    if config.mode == "long_short":
        for ticker, weight in inverse_volatility_weights(
            short, panel, signal_date, config.vol_lookback_days, config.max_weight
        ).items():
            weights[ticker] = weights.get(ticker, 0.0) - weight
    return weights


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------


@dataclass
class StrategyRun:
    view: FundamentalsView
    rebalances: list[Rebalance] = field(default_factory=list)
    nav_net: list[tuple[date, float]] = field(default_factory=list)
    nav_gross: list[tuple[date, float]] = field(default_factory=list)
    benchmark: list[tuple[date, float]] = field(default_factory=list)
    skipped: list[tuple[date, str]] = field(default_factory=list)


@dataclass(frozen=True)
class PerformanceSummary:
    cumulative_return: float
    cagr: float
    volatility: float
    sharpe: float | None
    max_drawdown: float
    avg_turnover: float
    n_rebalances: int


def simulate(
    rebalances: list[Rebalance],
    panel: PricePanel,
    config: MultifactorConfig,
    *,
    frictions: bool,
) -> list[tuple[date, float]]:
    """Daily mark-to-market from the first trade to the last session.

    Positions drift with prices between rebalances rather than being reset
    each day, so the drawdowns are the ones an investor would actually have
    lived through, not a series of month-end snapshots.
    """
    if not rebalances:
        return []

    targets = {r.trade_date: r.weights for r in rebalances}
    first_trade = rebalances[0].trade_date
    path = [d for d in panel.sessions if d >= first_trade]
    if not path:
        return []

    nav = 1.0
    exposure: dict[str, float] = {}
    curve: list[tuple[date, float]] = []
    borrow_daily = (config.borrow_bps_per_year / 10_000.0) / config.trading_days_per_year
    cost_rate = config.cost_bps_per_dollar / 10_000.0

    for i, day in enumerate(path):
        if i > 0:
            previous = path[i - 1]
            pnl = 0.0
            for ticker, value in list(exposure.items()):
                ret = panel.returns(ticker, previous, day)
                if ret is None:
                    continue
                pnl += value * ret
                exposure[ticker] = value * (1.0 + ret)
            nav += pnl
            if frictions:
                short_gross = sum(-v for v in exposure.values() if v < 0)
                nav -= short_gross * borrow_daily

        if day in targets and nav > 0:
            wanted = {t: w * nav for t, w in targets[day].items()}
            traded = sum(
                abs(wanted.get(t, 0.0) - exposure.get(t, 0.0))
                for t in set(wanted) | set(exposure)
            )
            if frictions:
                nav -= traded * cost_rate
            exposure = {t: w * nav for t, w in targets[day].items()}

        curve.append((day, nav))
        if nav <= 0:
            break
    return curve


def benchmark_curve(panel: PricePanel, tickers: list[str], start: date) -> list[tuple[date, float]]:
    """Equal-weighted, daily-rebalanced universe - the long-only comparison."""
    path = [d for d in panel.sessions if d >= start]
    nav = 1.0
    curve: list[tuple[date, float]] = []
    for i, day in enumerate(path):
        if i > 0:
            rets = [
                r
                for t in tickers
                if (r := panel.returns(t, path[i - 1], day)) is not None
            ]
            if rets:
                nav *= 1.0 + statistics.fmean(rets)
        curve.append((day, nav))
    return curve


def summarize(
    curve: list[tuple[date, float]], rebalances: list[Rebalance], trading_days: int = 252
) -> PerformanceSummary:
    if len(curve) < 2:
        return PerformanceSummary(0.0, 0.0, 0.0, None, 0.0, 0.0, len(rebalances))

    values = [v for _, v in curve]
    rets = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] > 0]
    cumulative = values[-1] / values[0] - 1.0

    years = max((curve[-1][0] - curve[0][0]).days / 365.25, 1e-9)
    cagr = (values[-1] / values[0]) ** (1 / years) - 1.0 if values[-1] > 0 else -1.0

    vol = statistics.pstdev(rets) * (trading_days**0.5) if len(rets) > 1 else 0.0
    mean = statistics.fmean(rets) * trading_days if rets else 0.0
    sharpe = mean / vol if vol > 0 else None

    peak, drawdown = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            drawdown = min(drawdown, value / peak - 1.0)

    turnovers = [r.turnover for r in rebalances[1:]]
    return PerformanceSummary(
        cumulative_return=cumulative,
        cagr=cagr,
        volatility=vol,
        sharpe=sharpe,
        max_drawdown=drawdown,
        avg_turnover=statistics.fmean(turnovers) if turnovers else 0.0,
        n_rebalances=len(rebalances),
    )


# --------------------------------------------------------------------------
# Information coefficients
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InformationCoefficient:
    signal: str
    mean_ic: float
    t_stat: float | None
    n_periods: int
    hit_rate: float


def information_coefficients(
    rebalances: list[Rebalance], panel: PricePanel
) -> list[InformationCoefficient]:
    """Each signal's rank correlation with next period's return.

    The equity curve mixes signal quality with sizing, costs and luck; the IC
    isolates whether the ordering itself carried information. A mean IC of
    0.02-0.05 is a useful equity signal, and |t| < 2 means this sample cannot
    tell it apart from zero.
    """
    per_signal: dict[str, list[float]] = {}
    for current, following in zip(rebalances, rebalances[1:], strict=False):
        forward = _forward_returns(panel, current.trade_date, following.trade_date)
        if len(forward) < 3:
            continue
        for signal in SIGNAL_NAMES:
            xs: list[float] = []
            ys: list[float] = []
            for ticker, signals in current.signals.items():
                if signal in signals and ticker in forward:
                    xs.append(signals[signal])
                    ys.append(forward[ticker])
            ic = spearman(xs, ys)
            if ic is not None:
                per_signal.setdefault(signal, []).append(ic)

    out: list[InformationCoefficient] = []
    for signal in SIGNAL_NAMES:
        series = per_signal.get(signal, [])
        if not series:
            continue
        mean = statistics.fmean(series)
        t_stat: float | None = None
        if len(series) > 1:
            spread = statistics.stdev(series)
            if spread > 0:
                t_stat = mean / (spread / len(series) ** 0.5)
        out.append(
            InformationCoefficient(
                signal=signal,
                mean_ic=mean,
                t_stat=t_stat,
                n_periods=len(series),
                hit_rate=sum(1 for v in series if v > 0) / len(series),
            )
        )
    return sorted(out, key=lambda r: r.mean_ic, reverse=True)


def _forward_returns(panel: PricePanel, start: date, end: date) -> dict[str, float]:
    out: dict[str, float] = {}
    for ticker in panel.adj_close:
        ret = panel.returns(ticker, start, end)
        if ret is not None:
            out[ticker] = ret
    return out


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def run_strategy(
    conn: psycopg.Connection,
    tickers: list[str],
    config: MultifactorConfig,
    sectors: dict[str, str],
    panel: PricePanel,
    signal_dates: list[date],
    view: FundamentalsView,
    latest_facts: pl.DataFrame,
    pit_cache: dict[date, pl.DataFrame],
) -> StrategyRun:
    """One full pass of the strategy under a single fundamentals view."""
    run = StrategyRun(view=view)
    previous_long: tuple[str, ...] = ()
    previous_short: tuple[str, ...] = ()
    previous_weights: dict[str, float] = {}

    for signal_date in signal_dates:
        trade_date = _next_session(panel, signal_date, config.execution_lag_sessions)
        if trade_date is None:
            run.skipped.append((signal_date, "no session available to trade on"))
            continue

        facts = make_fundamentals_view(
            view, pit_cache[signal_date], latest_facts, signal_date
        )
        signals_by_ticker: dict[str, dict[str, float]] = {}
        for ticker in tickers:
            fundamentals = build_ticker_fundamentals(facts, ticker)
            cap = compute_market_cap(fundamentals, panel, signal_date)
            values = compute_signals(
                fundamentals=fundamentals,
                cap=cap,
                price_history=panel.history(ticker, signal_date, 300),
                signal_date=signal_date,
                max_days_since_period_end=config.max_days_since_period_end,
                surprise_min_history=config.surprise_min_history,
                surprise_max_history=config.surprise_max_history,
            )
            excluded = config.excluded_signals(ticker)
            values = {k: v for k, v in values.items() if k not in excluded}
            if values:
                signals_by_ticker[ticker] = values

        scored = score_date(signals_by_ticker, sectors, config.scoring)
        if len(scored.composites) < config.min_scored:
            run.skipped.append(
                (signal_date, f"only {len(scored.composites)} names scored")
            )
            continue

        long = select_book(
            scored.composites, previous_long, config.entry_rank, config.hold_rank,
            short_side=False,
        )
        short = (
            select_book(
                scored.composites, previous_short, config.entry_rank, config.hold_rank,
                short_side=True,
            )
            if config.mode == "long_short"
            else ()
        )
        weights = target_weights(long, short, panel, signal_date, config)
        turnover = sum(
            abs(weights.get(t, 0.0) - previous_weights.get(t, 0.0))
            for t in set(weights) | set(previous_weights)
        ) / 2.0

        run.rebalances.append(
            Rebalance(
                signal_date=signal_date,
                trade_date=trade_date,
                long=long,
                short=short,
                weights=weights,
                scored=scored,
                signals=signals_by_ticker,
                turnover=turnover,
            )
        )
        previous_long, previous_short, previous_weights = long, short, weights

    run.nav_net = simulate(run.rebalances, panel, config, frictions=True)
    run.nav_gross = simulate(run.rebalances, panel, config, frictions=False)
    if run.rebalances:
        run.benchmark = benchmark_curve(panel, tickers, run.rebalances[0].trade_date)
    return run


def _next_session(panel: PricePanel, day: date, lag: int) -> date | None:
    index = panel.session_index(day)
    if index is None:
        return None
    target = index + lag
    return panel.sessions[target] if target < len(panel.sessions) else None


def load_point_in_time_facts(
    conn: psycopg.Connection, signal_dates: list[date]
) -> dict[date, pl.DataFrame]:
    """One point-in-time read per signal date, shared by all three views.

    The cutoff is 21:00 UTC - after the 16:00 ET close - so a filing that
    landed during the session is visible, and the availability lag stored on
    each fact decides whether it was actually tradeable.
    """
    from pdw.query import PointInTimeReader

    out: dict[date, pl.DataFrame] = {}
    for signal_date in signal_dates:
        as_of = datetime(signal_date.year, signal_date.month, signal_date.day, 21, tzinfo=UTC)
        out[signal_date] = PointInTimeReader(conn, as_of).fundamentals(METRICS)
    return out
