"""Turning seven raw signals into one ranked score per ticker.

The order matters: winsorize, then z-score, then average within a group,
then across groups, then sector-neutralise. Averaging raw signals instead
would let whichever one happens to be measured in the largest units decide
the ranking on its own.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GroupSpec:
    name: str
    weight: float
    signals: tuple[str, ...]


@dataclass(frozen=True)
class ScoringConfig:
    groups: tuple[GroupSpec, ...]
    min_groups: int = 3
    winsorize_pct: float = 5.0
    zscore_clip: float = 3.0
    min_tickers_per_signal: int = 10
    sector_neutralize: bool = True
    min_sector_size: int = 3


@dataclass
class ScoredDate:
    """Everything the report needs to explain one rebalance's ranking."""

    composites: dict[str, float] = field(default_factory=dict)
    zscores: dict[str, dict[str, float]] = field(default_factory=dict)  # signal -> ticker -> z
    group_scores: dict[str, dict[str, float]] = field(default_factory=dict)
    dropped_signals: tuple[str, ...] = ()


def winsorize(values: dict[str, float], pct: float) -> dict[str, float]:
    """Clip at the cross-sectional pct/100-pct percentiles.

    BRK.B's net income swings by tens of billions as its equity portfolio
    marks to market; one such print should not be allowed to define the
    entire cross-section's scale.
    """
    if not values or pct <= 0:
        return dict(values)
    ordered = sorted(values.values())
    low = _percentile(ordered, pct)
    high = _percentile(ordered, 100.0 - pct)
    if low > high:
        low, high = high, low
    return {ticker: min(max(value, low), high) for ticker, value in values.items()}


def _percentile(ordered: list[float], pct: float) -> float:
    """Linear-interpolation percentile of an already-sorted list."""
    if not ordered:
        raise ValueError("percentile of an empty sequence")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (pct / 100.0)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def zscore(values: dict[str, float], clip: float) -> dict[str, float]:
    """Cross-sectional z-score, clipped. A zero-spread signal scores flat."""
    if len(values) < 2:
        return {}
    mean = statistics.fmean(values.values())
    spread = statistics.pstdev(values.values())
    if spread <= 0:
        return {ticker: 0.0 for ticker in values}
    return {
        ticker: min(max((value - mean) / spread, -clip), clip) for ticker, value in values.items()
    }


def score_date(
    signals_by_ticker: dict[str, dict[str, float]],
    sectors: dict[str, str],
    config: ScoringConfig,
) -> ScoredDate:
    """Composite score per ticker for one rebalance date."""
    by_signal = _transpose(signals_by_ticker)

    zscores: dict[str, dict[str, float]] = {}
    dropped: list[str] = []
    for signal, values in by_signal.items():
        if len(values) < config.min_tickers_per_signal:
            # Too thin a cross-section to rank against: a z-score over six
            # names is noise wearing a statistic's clothes.
            dropped.append(signal)
            continue
        zscores[signal] = zscore(winsorize(values, config.winsorize_pct), config.zscore_clip)

    group_scores = _group_scores(zscores, config)
    composites = _composites(group_scores, config)
    if config.sector_neutralize:
        composites = sector_neutralize(composites, sectors, config.min_sector_size)

    return ScoredDate(
        composites=composites,
        zscores=zscores,
        group_scores=group_scores,
        dropped_signals=tuple(sorted(dropped)),
    )


def _transpose(signals_by_ticker: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for ticker, signals in signals_by_ticker.items():
        for name, value in signals.items():
            out.setdefault(name, {})[ticker] = value
    return out


def _group_scores(
    zscores: dict[str, dict[str, float]], config: ScoringConfig
) -> dict[str, dict[str, float]]:
    """Mean of a ticker's available z-scores within each group."""
    out: dict[str, dict[str, float]] = {}
    for group in config.groups:
        per_ticker: dict[str, list[float]] = {}
        for signal in group.signals:
            for ticker, value in zscores.get(signal, {}).items():
                per_ticker.setdefault(ticker, []).append(value)
        if per_ticker:
            out[group.name] = {t: statistics.fmean(v) for t, v in per_ticker.items()}
    return out


def _composites(
    group_scores: dict[str, dict[str, float]], config: ScoringConfig
) -> dict[str, float]:
    """Weighted mean of the groups a ticker actually has, if it has enough."""
    weights = {g.name: g.weight for g in config.groups}
    tickers = {t for scores in group_scores.values() for t in scores}

    composites: dict[str, float] = {}
    for ticker in tickers:
        available = [
            (weights[name], scores[ticker])
            for name, scores in group_scores.items()
            if ticker in scores
        ]
        if len(available) < config.min_groups:
            # Ranking a name on one group against names scored on four is
            # comparing different strategies, not different stocks.
            continue
        total_weight = sum(w for w, _ in available)
        if total_weight <= 0:
            continue
        composites[ticker] = sum(w * s for w, s in available) / total_weight
    return composites


def sector_neutralize(
    composites: dict[str, float], sectors: dict[str, str], min_sector_size: int
) -> dict[str, float]:
    """Subtract each sector's mean composite from its members.

    Without this, a value tilt across mega caps is mostly "long energy and
    banks, short software" - a sector bet, not a stock bet. Sectors too small
    to have a meaningful mean are left alone rather than zeroed out, which
    would force every member to exactly its sector average.
    """
    members: dict[str, list[str]] = {}
    for ticker in composites:
        members.setdefault(sectors.get(ticker, "unknown"), []).append(ticker)

    out = dict(composites)
    for sector, tickers in members.items():
        if sector == "unknown" or len(tickers) < min_sector_size:
            continue
        mean = statistics.fmean(composites[t] for t in tickers)
        for ticker in tickers:
            out[ticker] = composites[ticker] - mean
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation, the information coefficient's underlying statistic.

    Rank rather than Pearson because a factor's job is to *order* the
    cross-section; one runaway return should not dominate the measurement of
    whether the ordering was right.
    """
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mean_x, mean_y = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry, strict=True))
    den_x = sum((a - mean_x) ** 2 for a in rx)
    den_y = sum((b - mean_y) ** 2 for b in ry)
    if den_x <= 0 or den_y <= 0:
        return None
    return float(num / (den_x * den_y) ** 0.5)


def _ranks(values: list[float]) -> list[float]:
    """Average ranks, so ties don't manufacture an ordering that isn't there."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks
