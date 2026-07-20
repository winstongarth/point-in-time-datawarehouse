from pathlib import Path

import pytest

from pdw.metric_map import MetricMapping, load_metric_map

METRIC_MAP_PATH = Path(__file__).resolve().parent.parent / "config" / "metric_map.yaml"

EXPECTED_METRICS = {
    "revenue",
    "net_income",
    "total_assets",
    "total_equity",
    "shares_outstanding_diluted",
    "operating_cash_flow",
}


def test_loads_all_six_metrics() -> None:
    mapping = load_metric_map(METRIC_MAP_PATH)

    assert set(mapping) == EXPECTED_METRICS
    for metric in mapping.values():
        assert isinstance(metric, MetricMapping)
        assert metric.unit
        assert len(metric.tags) >= 1


def test_shares_outstanding_uses_shares_unit() -> None:
    mapping = load_metric_map(METRIC_MAP_PATH)

    assert mapping["shares_outstanding_diluted"].unit == "shares"


def test_revenue_has_priority_ordered_fallback_tags() -> None:
    mapping = load_metric_map(METRIC_MAP_PATH)

    assert mapping["revenue"].tags[0] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert len(mapping["revenue"].tags) > 1


def test_rejects_empty_file(tmp_path: Path) -> None:
    empty_file = tmp_path / "empty.yaml"
    empty_file.write_text("{}\n")

    with pytest.raises(ValueError, match="empty"):
        load_metric_map(empty_file)
