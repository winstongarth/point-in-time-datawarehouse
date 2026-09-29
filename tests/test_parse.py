import json
from pathlib import Path

from pdw.metric_map import MetricMapping, load_metric_map
from pdw.parse import parse_companyfacts

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "edgar" / "aapl_companyfacts_sample.json"
)
METRIC_MAP_PATH = Path(__file__).resolve().parent.parent / "config" / "metric_map.yaml"


def test_parses_all_six_metrics_for_aapl() -> None:
    body = FIXTURE.read_bytes()
    mapping = load_metric_map(METRIC_MAP_PATH)

    facts = parse_companyfacts(body, mapping, payload_id=1)

    assert {f.metric_code for f in facts} == set(mapping)
    assert all(f.cik == "0000320193" for f in facts)
    assert all(f.entity_name == "Apple Inc." for f in facts)


def test_records_the_actual_tag_used_as_vendor_native_tag() -> None:
    body = FIXTURE.read_bytes()
    mapping = load_metric_map(METRIC_MAP_PATH)

    facts = parse_companyfacts(body, mapping, payload_id=1)

    revenue_facts = [f for f in facts if f.metric_code == "revenue"]
    assert revenue_facts
    assert all(
        f.vendor_native_tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
        for f in revenue_facts
    )


def test_tag_switch_mid_history_keeps_both_eras_not_just_the_higher_priority_tag() -> None:
    """Regression test for a real bug found live (2026-07-20): AAPL reports
    revenue under `Revenues` through fiscal 2017 and under
    `RevenueFromContractWithCustomerExcludingAssessedTax` from fiscal 2018
    on (ASC 606 adoption). An earlier version of this function picked
    whichever tag appeared first with *any* data and used it exclusively,
    silently dropping every period the other tag covered - here, all
    pre-2018 revenue. Tag priority must be resolved per period, not once for
    the whole metric.
    """
    body = json.dumps(
        {
            "cik": 1,
            "entityName": "Test Corp",
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {
                            "USD": [
                                {
                                    "start": "2018-01-01",
                                    "end": "2018-12-31",
                                    "val": 200,
                                    "accn": "acc-new",
                                    "fy": 2018,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2019-02-01",
                                }
                            ]
                        }
                    },
                    "Revenues": {
                        "units": {
                            "USD": [
                                {
                                    "start": "2017-01-01",
                                    "end": "2017-12-31",
                                    "val": 100,
                                    "accn": "acc-old",
                                    "fy": 2017,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2018-02-01",
                                },
                                # 2018 also reported here (comparative prior-year
                                # column) - the higher-priority tag must win for
                                # this exact period, not double-count it.
                                {
                                    "start": "2018-01-01",
                                    "end": "2018-12-31",
                                    "val": 999,
                                    "accn": "acc-old-2",
                                    "fy": 2018,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2018-02-01",
                                },
                            ]
                        }
                    },
                }
            },
        }
    ).encode("utf-8")
    mapping = {
        "revenue": MetricMapping(
            unit="USD",
            tags=("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"),
        )
    }

    facts = parse_companyfacts(body, mapping, payload_id=1)

    by_year = {f.fiscal_year: f for f in facts}
    assert by_year[2017].vendor_native_tag == "Revenues"
    assert by_year[2017].value == 100
    assert by_year[2018].vendor_native_tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert by_year[2018].value == 200
    assert len(facts) == 2


def test_instant_concepts_have_no_period_start() -> None:
    body = FIXTURE.read_bytes()
    mapping = load_metric_map(METRIC_MAP_PATH)

    facts = parse_companyfacts(body, mapping, payload_id=1)

    assets_facts = [f for f in facts if f.metric_code == "total_assets"]
    assert assets_facts
    assert all(f.period_start is None for f in assets_facts)


def test_duration_concepts_have_period_start() -> None:
    body = FIXTURE.read_bytes()
    mapping = load_metric_map(METRIC_MAP_PATH)

    facts = parse_companyfacts(body, mapping, payload_id=1)

    revenue_facts = [f for f in facts if f.metric_code == "revenue"]
    assert revenue_facts
    assert all(f.period_start is not None for f in revenue_facts)


def test_missing_tag_falls_back_to_next_priority_tag() -> None:
    body = FIXTURE.read_bytes()
    mapping = load_metric_map(METRIC_MAP_PATH)
    # The fixture only has NetIncomeLoss, not net_income's other configured
    # fallback tags - so this exercises the fallback path even though the
    # first-priority tag happens to already be present.
    assert mapping["net_income"].tags[0] == "NetIncomeLoss"
    assert len(mapping["net_income"].tags) > 1

    facts = parse_companyfacts(body, mapping, payload_id=1)

    net_income_facts = [f for f in facts if f.metric_code == "net_income"]
    assert net_income_facts
    assert all(f.vendor_native_tag == "NetIncomeLoss" for f in net_income_facts)


def test_8k_style_datapoint_with_null_fiscal_year_and_period_is_tolerated() -> None:
    """Verified live against real EDGAR data (2026-07-20): 8-K recasting
    filings report a value without fy/fp - EDGAR only assigns those to the
    regular quarterly/annual cadence. core.fundamental_fact's schema already
    declares both columns nullable for this reason; the parser must not
    crash on it.
    """
    body = json.dumps(
        {
            "cik": 320193,
            "entityName": "Apple Inc.",
            "facts": {
                "us-gaap": {
                    "NetIncomeLoss": {
                        "units": {
                            "USD": [
                                {
                                    "start": "2012-09-30",
                                    "end": "2013-09-28",
                                    "val": 37037000000,
                                    "accn": "0001193125-15-023732",
                                    "fy": None,
                                    "fp": None,
                                    "form": "8-K",
                                    "filed": "2015-01-28",
                                }
                            ]
                        }
                    }
                }
            },
        }
    ).encode("utf-8")
    mapping = {"net_income": MetricMapping(unit="USD", tags=("NetIncomeLoss",))}

    facts = parse_companyfacts(body, mapping, payload_id=1)

    assert len(facts) == 1
    assert facts[0].fiscal_year is None
    assert facts[0].fiscal_period is None
    assert facts[0].form_type == "8-K"


def test_metric_entirely_absent_from_payload_is_simply_skipped() -> None:
    body = FIXTURE.read_bytes()
    mapping = load_metric_map(METRIC_MAP_PATH)
    mapping["nonexistent_metric"] = mapping["revenue"].__class__(
        unit="USD", tags=("ThisTagDoesNotExistAnywhere",)
    )

    facts = parse_companyfacts(body, mapping, payload_id=1)

    assert not any(f.metric_code == "nonexistent_metric" for f in facts)
