"""Rendering the multi-factor strategy's section of `docs/findings.md`.

The section is appended below the original experiment's report rather than
written to its own file, so one document carries both: the crude instrument
that measures look-ahead bias, and the strategy that has to live with it.
Both halves are generated, never hand-edited - `SECTION_MARKER` is the seam.
"""

from __future__ import annotations

import math
from datetime import date

from pdw.data import FundamentalsView
from pdw.multifactor import (
    InformationCoefficient,
    MultifactorConfig,
    PerformanceSummary,
    Rebalance,
    StrategyRun,
    summarize,
)

_SVG_WIDTH = 900
_SVG_HEIGHT = 360
_SVG_MARGIN = 50

_VIEW_COLORS = {
    FundamentalsView.POINT_IN_TIME: "#1f77b4",
    FundamentalsView.RESTATED_KNOWN_PERIODS: "#ff7f0e",
    FundamentalsView.LATEST_NAIVE: "#d62728",
}
_BENCHMARK_COLOR = "#7f7f7f"


def _pct(value: float) -> str:
    return f"{value:.2%}"


def _sharpe(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


def render_equity_curve_svg(
    curves: list[tuple[str, str, list[tuple[date, float]]]],
) -> str:
    """Hand-written multi-line SVG - same spirit as the hand-written SQL.

    `curves` is (label, colour, points). The y axis is **logarithmic**: the
    benchmark compounds to several times its start while the dollar-neutral
    book stays near 2x, and on a linear axis that squashes the three curves
    this chart exists to compare into the bottom fifth of the plot. On a log
    axis equal vertical distances are equal *returns*, which is the honest
    comparison between series of very different magnitudes.
    """
    points = [p for _, _, curve in curves for p in curve]
    if not points:
        return "<svg xmlns='http://www.w3.org/2000/svg'></svg>"

    dates = [d for d, _ in points]
    values = [v for _, v in points if v > 0]
    if not values:
        return "<svg xmlns='http://www.w3.org/2000/svg'></svg>"

    min_date, max_date = min(dates), max(dates)
    date_span = max((max_date - min_date).days, 1)

    log_min = math.log10(min(values + [1.0]))
    log_max = math.log10(max(values + [1.0]))
    log_span = max(log_max - log_min, 1e-9)

    plot_w = _SVG_WIDTH - 2 * _SVG_MARGIN
    plot_h = _SVG_HEIGHT - 2 * _SVG_MARGIN

    def _y(value: float) -> float:
        clamped = max(value, 10**log_min)
        return _SVG_MARGIN + (1 - (math.log10(clamped) - log_min) / log_span) * plot_h

    def _coords(curve: list[tuple[date, float]]) -> str:
        return " ".join(
            f"{_SVG_MARGIN + (d - min_date).days / date_span * plot_w:.1f},{_y(v):.1f}"
            for d, v in curve
            if v > 0
        )

    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_SVG_WIDTH} {_SVG_HEIGHT}" '
        'font-family="sans-serif" font-size="12">',
        f'<rect x="0" y="0" width="{_SVG_WIDTH}" height="{_SVG_HEIGHT}" fill="white" />',
        f'<text x="{_SVG_MARGIN}" y="22" font-size="14">Multi-factor strategy: '
        "net of costs, by fundamentals view</text>",
        f'<text x="{_SVG_MARGIN}" y="38" fill="#666">Growth of 1.0, log scale</text>',
    ]

    for level in _gridlines(10**log_min, 10**log_max):
        y = _y(level)
        emphasis = "#bbb" if level == 1.0 else "#eee"
        body.append(
            f'<line x1="{_SVG_MARGIN}" y1="{y:.1f}" x2="{_SVG_WIDTH - _SVG_MARGIN}" '
            f'y2="{y:.1f}" stroke="{emphasis}" stroke-dasharray="4,4" />'
        )
        body.append(
            f'<text x="{_SVG_WIDTH - _SVG_MARGIN + 6}" y="{y + 4:.1f}" fill="#666">'
            f"{level:g}x</text>"
        )

    for i, (label, color, curve) in enumerate(curves):
        dashed = ' stroke-dasharray="5,3"' if label == "Equal-weight universe" else ""
        body.append(
            f'<polyline points="{_coords(curve)}" fill="none" stroke="{color}" '
            f'stroke-width="2"{dashed} />'
        )
        legend_y = _SVG_MARGIN + 6 + i * 18
        body.append(
            f'<rect x="{_SVG_MARGIN + 10}" y="{legend_y}" width="12" height="12" fill="{color}" />'
        )
        body.append(f'<text x="{_SVG_MARGIN + 28}" y="{legend_y + 11}">{label}</text>')

    body += [
        f'<text x="{_SVG_MARGIN}" y="{_SVG_HEIGHT - 12}">{min_date.isoformat()}</text>',
        f'<text x="{_SVG_WIDTH - _SVG_MARGIN - 70}" y="{_SVG_HEIGHT - 12}">'
        f"{max_date.isoformat()}</text>",
        "</svg>",
    ]
    return "\n".join(body) + "\n"


def _gridlines(low: float, high: float) -> list[float]:
    """Round multiples spanning the range, always including the 1.0 baseline."""
    candidates = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0]
    return [c for c in candidates if low <= c <= high] or [1.0]


def render_report(
    runs: dict[FundamentalsView, StrategyRun],
    ics: list[InformationCoefficient],
    config: MultifactorConfig,
    chart_path: str,
    compare: bool,
) -> str:
    primary = runs[FundamentalsView.POINT_IN_TIME]
    net = summarize(primary.nav_net, primary.rebalances, config.trading_days_per_year)
    gross = summarize(primary.nav_gross, primary.rebalances, config.trading_days_per_year)
    bench = summarize(primary.benchmark, [], config.trading_days_per_year)

    first = primary.rebalances[0].trade_date if primary.rebalances else None
    last = primary.nav_net[-1][0] if primary.nav_net else None
    span = f"{first.isoformat()} to {last.isoformat()}" if first and last else "no periods"

    lines = [
        "# Strategy: multi-factor earnings long/short",
        "",
        "The backtest above is a deliberately crude instrument for *measuring* look-ahead "
        "bias. This is the other half: a strategy built on the same warehouse that could "
        f"plausibly be traded. It ranks the universe {config.rebalance} on four families of "
        "signals derived from earnings reports and prices, holds a "
        f"{'dollar-neutral long/short' if config.mode == 'long_short' else 'long-only'} book, "
        "and pays realistic costs. Every fundamentals read goes through `PointInTimeReader` "
        "with an `as_of` of the signal date's close.",
        "",
        "**The one-session gap between signal and trade is deliberate:** a signal that uses "
        "today's close cannot also trade at today's close. Signals are computed at each "
        "period-end close and the book is traded at the *next* session's close, paying "
        f"{config.cost_bps_per_dollar:.0f} bps on every dollar moved.",
        "",
        f"Run live over **{span}**, {net.n_rebalances} rebalances.",
        "",
        "## Performance",
        "",
        "| Metric | Gross | Net of costs | Equal-weight universe |",
        "|---|---|---|---|",
        f"| Cumulative return | {_pct(gross.cumulative_return)} | "
        f"{_pct(net.cumulative_return)} | {_pct(bench.cumulative_return)} |",
        f"| CAGR | {_pct(gross.cagr)} | {_pct(net.cagr)} | {_pct(bench.cagr)} |",
        f"| Volatility (annualized) | {_pct(gross.volatility)} | {_pct(net.volatility)} | "
        f"{_pct(bench.volatility)} |",
        f"| Sharpe | {_sharpe(gross.sharpe)} | {_sharpe(net.sharpe)} | "
        f"{_sharpe(bench.sharpe)} |",
        f"| Max drawdown | {_pct(gross.max_drawdown)} | {_pct(net.max_drawdown)} | "
        f"{_pct(bench.max_drawdown)} |",
        f"| Avg. turnover per rebalance | {_pct(gross.avg_turnover)} | "
        f"{_pct(net.avg_turnover)} | n/a |",
        "",
        "**Compare Sharpe, not CAGR, against the benchmark.** A dollar-neutral book is "
        "built not to depend on the market's direction, so its return is not trying to beat "
        "a long-only index; the question is how much return it produces per unit of risk. "
        "The gross-to-net gap is how much of any edge survives trading costs.",
        "",
        f"![Multi-factor equity curves]({chart_path})",
        "",
    ]

    if primary.rebalances:
        lines += latest_rebalance_detail(primary.rebalances[-1])

    if compare:
        lines += _render_decomposition(runs, config)

    lines += _render_ic_table(ics)
    lines += _render_construction(config, primary)
    return "\n".join(lines)


def _render_decomposition(
    runs: dict[FundamentalsView, StrategyRun], config: MultifactorConfig
) -> list[str]:
    lines = [
        "## Look-ahead decomposition",
        "",
        "The identical strategy, run on three views of the *same* fundamentals. Only what "
        "the strategy is allowed to know changes; the schedule, sizing and costs are held "
        "fixed, so the gap between consecutive rows isolates one effect at a time.",
        "",
        "| View | What it sees | Cumulative | Sharpe | Max DD | Isolates |",
        "|---|---|---|---|---|---|",
    ]
    descriptions = {
        FundamentalsView.POINT_IN_TIME: "Only what was filed before the cutoff, as then-stated",
        FundamentalsView.RESTATED_KNOWN_PERIODS: (
            "Today's revised values, but only for quarters already reported"
        ),
        FundamentalsView.LATEST_NAIVE: (
            "Today's values for every quarter ended by the signal date, filed or not"
        ),
    }
    summaries: dict[FundamentalsView, PerformanceSummary] = {}
    for view in FundamentalsView:
        run = runs.get(view)
        if run is None:
            continue
        summary = summarize(run.nav_net, run.rebalances, config.trading_days_per_year)
        summaries[view] = summary
        lines.append(
            f"| {view.label} | {descriptions[view]} | {_pct(summary.cumulative_return)} | "
            f"{_sharpe(summary.sharpe)} | {_pct(summary.max_drawdown)} | {view.measures} |"
        )

    lines += [
        "",
        "Row 2 minus row 1 is **restatement bias** alone: the same quarters, revalued by "
        "later amendments. Row 3 minus row 2 is **reporting-lag look-ahead**: trading a "
        "quarter weeks before its 10-Q existed. The original experiment above conflates "
        "the two - its \"latest\" run includes the quarter that ended the day before each "
        "rebalance, so its 217 position differences mix both effects.",
        "",
    ]
    lines += _interpret_decomposition(summaries)
    return lines


def _interpret_decomposition(
    summaries: dict[FundamentalsView, PerformanceSummary],
) -> list[str]:
    """State which way the gaps actually ran, rather than leaving it implied.

    The expected shape is that look-ahead *flatters* a backtest - knowing a
    quarter before it is filed means trading the post-earnings drift before it
    starts. Whether real data agrees is the question, and it is worth saying
    plainly when it does not.
    """
    pit = summaries.get(FundamentalsView.POINT_IN_TIME)
    restated = summaries.get(FundamentalsView.RESTATED_KNOWN_PERIODS)
    naive = summaries.get(FundamentalsView.LATEST_NAIVE)
    if pit is None or restated is None or naive is None:
        return []
    if pit.sharpe is None or restated.sharpe is None or naive.sharpe is None:
        return []

    restatement_gap = restated.sharpe - pit.sharpe
    lag_gap = naive.sharpe - restated.sharpe
    total_gap = naive.sharpe - pit.sharpe

    lines = ["### Which way the gaps actually ran", ""]
    if total_gap < 0:
        lines += [
            "**Both forms of look-ahead made this strategy look *worse*, not better** - "
            f"Sharpe falls from {pit.sharpe:.2f} point-in-time to {naive.sharpe:.2f} on the "
            f"naive view ({restatement_gap:+.2f} from restatement, {lag_gap:+.2f} from "
            "reporting lag). That is the opposite of the usual expectation, which is that "
            "seeing a quarter before it was filed lets a backtest trade the "
            "post-earnings drift before it starts, flattering the result.",
            "",
            "The honest reading is that **this is what look-ahead does to a strategy whose "
            "signals carry no measurable edge**. Every information coefficient below has "
            "|t| < 2, so the ranking is close to noise; feeding it *different* noise - "
            "earlier, restated - reshuffles the book without making it more right. The "
            "three curves are three draws from much the same distribution, and their order "
            "is not something this sample can resolve. Read the gaps as evidence about how "
            "much the *positions* move, not as a measurement of how much edge look-ahead "
            "manufactures: on a strategy that did have an edge, the sign would be "
            "expected to flip.",
        ]
    else:
        lines += [
            f"**Look-ahead flattered the strategy, as expected** - Sharpe rises from "
            f"{pit.sharpe:.2f} point-in-time to {naive.sharpe:.2f} on the naive view "
            f"({restatement_gap:+.2f} from restatement, {lag_gap:+.2f} from reporting lag). "
            "The larger share coming from reporting lag is the usual shape: knowing a "
            "quarter before its 10-Q exists means trading the post-earnings drift before "
            "it starts, which is a much bigger effect than revaluing a quarter already "
            "reported.",
            "",
            "Weigh this against the information coefficients below: if no signal reaches "
            "|t| > 2, the gap between these curves is not cleanly separable from noise.",
        ]
    lines.append("")
    return lines


def _render_ic_table(ics: list[InformationCoefficient]) -> list[str]:
    lines = [
        "## Information coefficients",
        "",
        "Each signal's rank correlation with the *next* period's return, averaged across "
        "rebalances. The equity curve mixes signal quality with sizing, costs and luck; the "
        "IC isolates whether the ordering itself carried information. A mean IC of "
        "0.02-0.05 is a useful equity signal; **|t| < 2 means this sample cannot tell it "
        "apart from zero**, which on 50 names over under ten years is the common case.",
        "",
        "| Signal | Mean IC | t-stat | Periods | Hit rate |",
        "|---|---|---|---|---|",
    ]
    for ic in ics:
        t_stat = f"{ic.t_stat:.2f}" if ic.t_stat is not None else "n/a"
        lines.append(
            f"| `{ic.signal}` | {ic.mean_ic:+.4f} | {t_stat} | {ic.n_periods} | "
            f"{ic.hit_rate:.0%} |"
        )
    lines += [
        "",
        "Hit rate is the share of rebalances where the signal's IC was positive; 50% is a "
        "coin flip.",
        "",
    ]
    return lines


def _render_construction(config: MultifactorConfig, run: StrategyRun) -> list[str]:
    groups = ", ".join(
        f"**{g.name}** ({', '.join('`' + s + '`' for s in g.signals)})"
        for g in config.scoring.groups
    )
    lines = [
        "## How a position gets made",
        "",
        f"1. **Signals.** Four equally-weighted groups: {groups}. Every signal is oriented "
        "so higher is better, which is why `low_accruals` is negated.",
        f"2. **Winsorize** at the {config.scoring.winsorize_pct:.0f}th/"
        f"{100 - config.scoring.winsorize_pct:.0f}th cross-sectional percentile, then "
        f"**z-score** clipped at ±{config.scoring.zscore_clip:.0f}. A signal held by fewer "
        f"than {config.scoring.min_tickers_per_signal} names is dropped for that date.",
        f"3. **Composite** = weighted mean of the group scores a ticker actually has; it "
        f"needs {config.scoring.min_groups} of 4 groups to be ranked at all.",
        f"4. **Sector-neutralise** against `config/sectors.yaml` (sectors with "
        f"{config.scoring.min_sector_size}+ scored names). Without this, a value tilt on "
        "mega caps is mostly \"long energy and banks, short software\" - a sector bet, not a "
        "stock bet.",
        f"5. **Book.** Long the top {config.entry_rank}, short the bottom "
        f"{config.entry_rank}, but a name is *held* while it stays inside the top/bottom "
        f"{config.hold_rank}. Without that buffer, names around rank {config.entry_rank} "
        "flip every month and the strategy pays to trade noise.",
        f"6. **Size** inversely to {config.vol_lookback_days}-day realised volatility, "
        f"capped at {config.max_weight:.0%} per name, so one high-volatility name doesn't "
        "carry several times the risk of a staple.",
        "",
        "### Where the numbers come from, and the traps avoided",
        "",
        "- **Quarters are rebuilt, not read.** EDGAR almost never tags Q4 on its own (a "
        "10-K reports the fiscal year) and cash-flow statements are year-to-date in every "
        "10-Q. `pdw.quarterly` keeps a directly-reported 3-month fact when one exists and "
        "otherwise derives the quarter from two cumulative facts sharing a fiscal-year "
        "start: Q4 = FY − 9M, Q2 cash flow = 6M − 3M. A TTM is only computed over **four "
        "consecutive** quarters, checked rather than assumed.",
        "- **Market cap is split-consistent.** A filing's share count is in the units of "
        "its period end; the price is on the signal date. `pdw.signals.market_cap` rebases "
        "the count by the split factor accumulated between those two dates, recovered as "
        "`tiingo_close / yfinance_close` on each day - exactly the cumulative split factor "
        "after that day.",
        "- **Share counts stay point-in-time even in the restated runs**, because later "
        "filings restate historical counts onto a post-split basis, which would move market "
        "cap mechanically rather than economically.",
        f"- **Stale data is dropped.** A company whose latest quarter ended more than "
        f"{config.max_days_since_period_end} days before the signal date gets no "
        "fundamental signals that period - but keeps its momentum signal, which needs no "
        "filing.",
    ]
    for exclusion in config.exclusions:
        tickers = ", ".join(sorted(exclusion.tickers))
        signals = ", ".join(f"`{s}`" for s in sorted(exclusion.signals))
        lines.append(
            f"- **Business-model exclusions** ({tickers}): {signals} dropped. "
            f"{exclusion.reason}"
        )

    lines += [
        "",
        "### Frictions modelled, and not",
        "",
        "| Modelled | Not modelled |",
        "|---|---|",
        f"| {config.execution_lag_sessions}-session execution lag | Market impact growing "
        "with trade size (immaterial at mega-cap liquidity for a personal book, material at "
        "fund scale) |",
        f"| {config.cost_bps_per_dollar:.0f} bps per dollar traded | Interest on cash and "
        "the short rebate (understates a real long/short by roughly the T-bill rate) |",
        f"| {config.borrow_bps_per_year:.0f} bps/yr borrow on shorts | Hard-to-borrow "
        "names, recalls |",
        "| Positions drift with prices between rebalances | Taxes, dividends withheld on "
        "shorts |",
        "| Daily mark-to-market, so drawdowns are real | Intraday execution |",
        "",
        "### Honest framing",
        "",
        "These are the most widely published equity factors, and published factors have "
        "weakened after publication (McLean & Pontiff 2016). On 50 mega caps they are also "
        "crowded, and the universe is survivorship-biased by construction "
        "(see [limitations.md](limitations.md)) - today's 50 largest names are all "
        "long-run winners. Treat this as a disciplined framework for *testing* the factors "
        "on point-in-time data, not as a claim that they still work.",
        "",
        "Known gaps, in rough order of how much they matter: fundamentals become usable at "
        "the 10-Q/10-K filing, often days to weeks after the earnings press release (8-K "
        "Item 2.02), so the strategy is conservative but slow and misses part of the drift; "
        "surprise is measured against the same quarter last year rather than analyst "
        "consensus; gross profit and diluted EPS are not in `config/metric_map.yaml` yet; "
        "and sectors are today's classification applied to all of history.",
        "",
    ]
    if run.skipped:
        shown = ", ".join(f"{d.isoformat()} ({why})" for d, why in run.skipped[:5])
        more = f" and {len(run.skipped) - 5} more" if len(run.skipped) > 5 else ""
        lines += [
            f"**Skipped rebalances:** {len(run.skipped)} - {shown}{more}.",
            "",
        ]
    return lines


def upsert_section(existing: str, section: str, marker: str) -> str:
    """Replace everything from `marker` onward with a freshly rendered section.

    Keeps the original findings report above the marker untouched, so the two
    halves of the document can be regenerated independently.
    """
    head = existing.split(marker)[0].rstrip()
    return f"{head}\n\n{marker}\n\n{section.rstrip()}\n"


def strip_section(existing: str, marker: str) -> str:
    """Everything below the marker, marker included - or empty if absent."""
    if marker not in existing:
        return ""
    return marker + existing.split(marker, 1)[1]


def latest_rebalance_detail(rebalance: Rebalance, limit: int = 10) -> list[str]:
    """A readable snapshot of the most recent book, for the report's tail."""
    lines = [
        "## The most recent book",
        "",
        f"Signalled at the {rebalance.signal_date.isoformat()} close and traded at the "
        f"{rebalance.trade_date.isoformat()} close. Weights are signed shares of NAV, so "
        "each leg sums to 100%. The composite is the sector-neutralised score the ranking "
        "is built on - it has no units, only an ordering.",
        "",
        "| Side | Ticker | Weight | Composite |",
        "|---|---|---|---|",
    ]
    for ticker in rebalance.long[:limit]:
        lines.append(
            f"| long | {ticker} | {rebalance.weights.get(ticker, 0.0):+.2%} | "
            f"{rebalance.scored.composites.get(ticker, float('nan')):+.2f} |"
        )
    for ticker in rebalance.short[:limit]:
        lines.append(
            f"| short | {ticker} | {rebalance.weights.get(ticker, 0.0):+.2%} | "
            f"{rebalance.scored.composites.get(ticker, float('nan')):+.2f} |"
        )
    lines.append("")
    return lines


__all__ = [
    "PerformanceSummary",
    "latest_rebalance_detail",
    "render_equity_curve_svg",
    "render_report",
    "strip_section",
    "upsert_section",
]
