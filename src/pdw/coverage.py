from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from pdw.parse import ParsedFact


@dataclass(frozen=True)
class CoverageGap:
    cik: str
    ticker: str
    fiscal_year: int
    fiscal_period: str
    missing_metrics: tuple[str, ...]


@dataclass(frozen=True)
class CoverageReport:
    total_entity_quarters: int
    fully_covered: int
    coverage_pct: float
    gaps: tuple[CoverageGap, ...]


def compute_coverage(
    facts: list[ParsedFact], ticker_by_cik: dict[str, str], expected_metrics: frozenset[str]
) -> CoverageReport:
    """Target: all 6 metrics present for >=90% of
    "entity-quarters" - every (cik, fiscal_year, fiscal_period) combination
    that appears anywhere in the parsed facts, regardless of which metric
    produced it.

    Facts with no fiscal_year/fiscal_period are excluded from this
    aggregation - they have nothing to be grouped into. In practice these
    are 8-K recasting filings, which report a value without EDGAR assigning
    it to the regular quarterly/annual cadence; they still land in stg in
    full, just outside this quarter-keyed coverage view.
    """
    present_by_key: dict[tuple[str, int, str], set[str]] = defaultdict(set)
    for fact in facts:
        if fact.fiscal_year is None or fact.fiscal_period is None:
            continue
        present_by_key[(fact.cik, fact.fiscal_year, fact.fiscal_period)].add(fact.metric_code)

    gaps: list[CoverageGap] = []
    fully_covered = 0
    for (cik, fiscal_year, fiscal_period), present in present_by_key.items():
        missing = expected_metrics - present
        if missing:
            gaps.append(
                CoverageGap(
                    cik=cik,
                    ticker=ticker_by_cik.get(cik, "?"),
                    fiscal_year=fiscal_year,
                    fiscal_period=fiscal_period,
                    missing_metrics=tuple(sorted(missing)),
                )
            )
        else:
            fully_covered += 1

    total = len(present_by_key)
    coverage_pct = (fully_covered / total * 100) if total else 0.0
    gaps.sort(key=lambda g: (g.ticker, g.fiscal_year, g.fiscal_period))
    return CoverageReport(
        total_entity_quarters=total,
        fully_covered=fully_covered,
        coverage_pct=coverage_pct,
        gaps=tuple(gaps),
    )


def render_coverage_report(report: CoverageReport) -> str:
    lines = [
        "# Fundamentals Coverage Report",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        "",
        f"**Coverage: {report.fully_covered}/{report.total_entity_quarters} "
        f"entity-quarters ({report.coverage_pct:.1f}%) have all 6 metrics present.**",
        "",
    ]

    if not report.gaps:
        lines.append("No gaps.")
        return "\n".join(lines) + "\n"

    lines += [
        f"## Gaps ({len(report.gaps)})",
        "",
        "| CIK | Ticker | Fiscal Year | Fiscal Period | Missing Metrics |",
        "|---|---|---|---|---|",
    ]
    for gap in report.gaps:
        missing = ", ".join(gap.missing_metrics)
        lines.append(
            f"| {gap.cik} | {gap.ticker} | {gap.fiscal_year} | {gap.fiscal_period} | {missing} |"
        )
    return "\n".join(lines) + "\n"
