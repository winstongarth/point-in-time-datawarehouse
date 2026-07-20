from datetime import date

from pdw.coverage import compute_coverage, render_coverage_report
from pdw.parse import ParsedFact

METRICS = frozenset({"revenue", "net_income", "total_assets"})


def _fact(cik: str, metric: str, fy: int | None = 2025, fp: str | None = "Q1") -> ParsedFact:
    return ParsedFact(
        cik=cik,
        entity_name="Test Corp",
        metric_code=metric,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 3, 31),
        fiscal_year=fy,
        fiscal_period=fp,
        value=1.0,
        unit="USD",
        vendor_native_tag="SomeTag",
        form_type="10-Q",
        accession_no="0000000000-25-000001",
        filed_date=date(2025, 4, 1),
        payload_id=1,
    )


def test_fully_covered_entity_quarter_has_no_gap() -> None:
    facts = [_fact("A", m) for m in METRICS]

    report = compute_coverage(facts, {"A": "AAA"}, METRICS)

    assert report.total_entity_quarters == 1
    assert report.fully_covered == 1
    assert report.coverage_pct == 100.0
    assert report.gaps == ()


def test_missing_metric_is_named_as_a_gap() -> None:
    facts = [_fact("A", "revenue"), _fact("A", "net_income")]  # total_assets missing

    report = compute_coverage(facts, {"A": "AAA"}, METRICS)

    assert report.fully_covered == 0
    assert len(report.gaps) == 1
    assert report.gaps[0].missing_metrics == ("total_assets",)
    assert report.gaps[0].ticker == "AAA"


def test_coverage_percentage_across_multiple_entity_quarters() -> None:
    facts = [
        *[_fact("A", m, fy=2025, fp="Q1") for m in METRICS],  # fully covered
        *[_fact("A", m, fy=2025, fp="Q2") for m in METRICS if m != "total_assets"],  # gap
    ]

    report = compute_coverage(facts, {"A": "AAA"}, METRICS)

    assert report.total_entity_quarters == 2
    assert report.fully_covered == 1
    assert report.coverage_pct == 50.0


def test_facts_without_fiscal_period_are_excluded_from_aggregation() -> None:
    """8-K style facts (no fy/fp) can't be grouped into an entity-quarter -
    they must not crash the aggregation or silently count as a gap."""
    facts = [
        *[_fact("A", m) for m in METRICS],  # fully covered Q1
        _fact("A", "revenue", fy=None, fp=None),  # unrelated 8-K datapoint
    ]

    report = compute_coverage(facts, {"A": "AAA"}, METRICS)

    assert report.total_entity_quarters == 1
    assert report.fully_covered == 1
    assert report.gaps == ()


def test_unknown_ticker_renders_as_placeholder() -> None:
    facts = [_fact("UNKNOWN_CIK", "revenue")]

    report = compute_coverage(facts, {}, METRICS)

    assert report.gaps[0].ticker == "?"


def test_render_no_gaps() -> None:
    report = compute_coverage([_fact("A", m) for m in METRICS], {"A": "AAA"}, METRICS)

    text = render_coverage_report(report)

    assert "100.0%" in text
    assert "No gaps." in text


def test_render_with_gaps_includes_table_row() -> None:
    facts = [_fact("A", "revenue")]

    report = compute_coverage(facts, {"A": "AAA"}, METRICS)
    text = render_coverage_report(report)

    assert "| A | AAA | 2025 | Q1 |" in text
