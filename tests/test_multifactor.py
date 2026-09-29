"""Tests for the multi-factor strategy. No database needed.

Every fixture here is hand-built, which is exactly the limitation this
project keeps rediscovering (see docs/postmortems.md) - so the cases chosen
are the *shapes* real filings take that a naive implementation gets wrong,
not just round numbers that happen to divide evenly.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from pdw.data import FundamentalsView, make_fundamentals_view
from pdw.multifactor import (
    Exclusion,
    MultifactorConfig,
    PricePanel,
    Rebalance,
    _apply_cap,
    inverse_volatility_weights,
    rebalance_sessions,
    select_book,
    simulate,
    summarize,
)
from pdw.multifactor_report import (
    latest_rebalance_detail,
    strip_section,
    upsert_section,
)
from pdw.quarterly import (
    RawDurationFact,
    classify_shape,
    quarterly_series,
    same_quarter_last_year,
    trailing_twelve_months,
    ttm_value,
)
from pdw.scoring import (
    GroupSpec,
    ScoringConfig,
    score_date,
    sector_neutralize,
    spearman,
    winsorize,
    zscore,
)
from pdw.signals import (
    earnings_surprise,
    low_accruals,
    market_cap,
    momentum_12_1,
    return_on_assets,
    split_adjustment_factor,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fact(start: date, end: date, value: float, fact_id: int = 1) -> RawDurationFact:
    return RawDurationFact(
        period_start=start,
        period_end=end,
        value=value,
        filed_date=end,
        fact_id=fact_id,
        accession_no=f"acc-{fact_id}",
    )


def _scoring_config(**overrides: object) -> ScoringConfig:
    defaults: dict[str, object] = {
        "groups": (
            GroupSpec("value", 1.0, ("earnings_yield", "cash_flow_yield")),
            GroupSpec("growth", 1.0, ("revenue_growth", "earnings_surprise")),
            GroupSpec("quality", 1.0, ("return_on_assets", "low_accruals")),
            GroupSpec("momentum", 1.0, ("momentum_12_1",)),
        ),
        "min_groups": 3,
        "min_tickers_per_signal": 3,
        "sector_neutralize": False,
    }
    defaults.update(overrides)
    return ScoringConfig(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# quarterly reconstruction
# ---------------------------------------------------------------------------


def test_classify_shape_covers_52_53_week_fiscal_calendars() -> None:
    """COST files 12-week quarters with a 16-week Q4, and 24/36-week cumulatives.

    A band built from calendar arithmetic (90/180/270/365) drops all four,
    which silently costs such a filer most of its cash-flow series.
    """
    assert classify_shape(83) == "Q1"
    assert classify_shape(111) == "Q1"  # 16-week Q4
    assert classify_shape(167) == "H1"  # 24 weeks
    assert classify_shape(251) == "M9"  # 36 weeks
    assert classify_shape(364) == "FY"  # 52 weeks
    assert classify_shape(500) is None


def test_directly_reported_quarter_is_preferred_over_a_derived_one() -> None:
    facts = [
        _fact(date(2024, 1, 1), date(2024, 3, 31), 100.0, 1),
        _fact(date(2024, 1, 1), date(2024, 6, 30), 250.0, 2),
        _fact(date(2024, 4, 1), date(2024, 6, 30), 150.0, 3),  # direct Q2
    ]
    quarters = quarterly_series(facts)
    q2 = next(q for q in quarters if q.period_end == date(2024, 6, 30))
    assert q2.derived is False
    assert q2.value == 150.0
    assert q2.fact_id == 3


def test_q2_is_derived_from_the_half_year_minus_the_first_quarter() -> None:
    """The regression that matters most: a 3-month fact is *also* the
    shortest cumulative of its fiscal year.

    Filing it only as a reported quarter leaves the half-year fact with no
    predecessor to difference against, so every filer whose cash-flow
    statement is year-to-date loses every quarter after its first.
    """
    facts = [
        _fact(date(2024, 1, 1), date(2024, 3, 31), 100.0, 1),  # Q1 (3m)
        _fact(date(2024, 1, 1), date(2024, 6, 30), 250.0, 2),  # H1 (6m), no direct Q2
    ]
    quarters = quarterly_series(facts)
    q2 = next(q for q in quarters if q.period_end == date(2024, 6, 30))
    assert q2.derived is True
    assert q2.value == pytest.approx(150.0)
    assert q2.period_start == date(2024, 3, 31)


def test_q4_is_derived_from_the_fiscal_year_minus_nine_months() -> None:
    """EDGAR almost never tags Q4: a 10-K reports the fiscal year."""
    facts = [
        _fact(date(2024, 1, 1), date(2024, 3, 31), 100.0, 1),
        _fact(date(2024, 1, 1), date(2024, 6, 30), 250.0, 2),
        _fact(date(2024, 1, 1), date(2024, 9, 30), 400.0, 3),
        _fact(date(2024, 1, 1), date(2024, 12, 31), 600.0, 4),
    ]
    quarters = quarterly_series(facts)
    q4 = next(q for q in quarters if q.period_end == date(2024, 12, 31))
    assert q4.derived is True
    assert q4.value == pytest.approx(200.0)
    assert ttm_value(quarters, date(2024, 12, 31)) == pytest.approx(600.0)


def test_derived_quarter_is_only_knowable_once_both_its_inputs_are() -> None:
    nine_month = _fact(date(2024, 1, 1), date(2024, 9, 30), 400.0, 3)
    full_year = RawDurationFact(
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        value=600.0,
        filed_date=date(2025, 2, 1),
        fact_id=4,
        accession_no="acc-4",
    )
    quarters = quarterly_series([nine_month, full_year])
    q4 = next(q for q in quarters if q.period_end == date(2024, 12, 31))
    assert q4.filed_date == date(2025, 2, 1)


def test_ttm_refuses_a_chain_with_a_missing_quarter() -> None:
    """A gap must return None, not a sum spanning fifteen months."""
    facts = [
        _fact(date(2023, 1, 1), date(2023, 3, 31), 10.0, 1),
        _fact(date(2023, 4, 1), date(2023, 6, 30), 20.0, 2),
        # Q3 missing entirely
        _fact(date(2023, 10, 1), date(2023, 12, 31), 40.0, 4),
        _fact(date(2024, 1, 1), date(2024, 3, 31), 50.0, 5),
    ]
    quarters = quarterly_series(facts)
    assert trailing_twelve_months(quarters, date(2024, 3, 31)) is None
    assert ttm_value(quarters, date(2024, 3, 31)) is None


def test_ttm_accepts_four_consecutive_quarters() -> None:
    facts = [
        _fact(date(2023, 1, 1), date(2023, 3, 31), 10.0, 1),
        _fact(date(2023, 4, 1), date(2023, 6, 30), 20.0, 2),
        _fact(date(2023, 7, 1), date(2023, 9, 30), 30.0, 3),
        _fact(date(2023, 10, 1), date(2023, 12, 31), 40.0, 4),
    ]
    quarters = quarterly_series(facts)
    chain = trailing_twelve_months(quarters, date(2023, 12, 31))
    assert chain is not None
    assert [q.period_end for q in chain] == [
        date(2023, 3, 31),
        date(2023, 6, 30),
        date(2023, 9, 30),
        date(2023, 12, 31),
    ]
    assert ttm_value(quarters, date(2023, 12, 31)) == pytest.approx(100.0)


def test_same_quarter_last_year_matches_seasonally_not_sequentially() -> None:
    facts = [
        _fact(date(2023, 10, 1), date(2023, 12, 31), 40.0, 1),
        _fact(date(2024, 7, 1), date(2024, 9, 30), 30.0, 2),
        _fact(date(2024, 10, 1), date(2024, 12, 31), 44.0, 3),
    ]
    quarters = quarterly_series(facts)
    current = next(q for q in quarters if q.period_end == date(2024, 12, 31))
    prior = same_quarter_last_year(quarters, current)
    assert prior is not None
    assert prior.period_end == date(2023, 12, 31)


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def test_split_factor_is_the_ratio_of_raw_to_split_adjusted_close() -> None:
    """Before a 10:1 split, the raw quote is ten times the adjusted one."""
    assert split_adjustment_factor(1000.0, 100.0) == pytest.approx(10.0)
    assert split_adjustment_factor(120.0, 120.0) == pytest.approx(1.0)
    assert split_adjustment_factor(None, 100.0) is None
    assert split_adjustment_factor(100.0, 0.0) is None


def test_market_cap_rebases_a_pre_split_share_count() -> None:
    """A share count filed before a 10:1 split is in pre-split units.

    Multiplying it by a post-split price understates market cap tenfold -
    the trap that makes a split name look absurdly cheap on every value
    signal in the month after it splits.
    """
    cap = market_cap(
        shares=1_000_000.0,
        shares_period_end=date(2024, 3, 31),
        price_on_signal_date=50.0,
        factor_at_period_end=10.0,  # a 10:1 split happened after the filing
        factor_at_signal_date=1.0,  # ...and before the signal date
    )
    assert cap is not None
    assert cap.shares == pytest.approx(10_000_000.0)
    assert cap.value == pytest.approx(500_000_000.0)


def test_market_cap_leaves_an_unsplit_count_alone() -> None:
    cap = market_cap(
        shares=2_000.0,
        shares_period_end=date(2024, 3, 31),
        price_on_signal_date=10.0,
        factor_at_period_end=3.0,
        factor_at_signal_date=3.0,  # same factor: no split in between
    )
    assert cap is not None
    assert cap.split_factor == pytest.approx(1.0)
    assert cap.value == pytest.approx(20_000.0)


def test_market_cap_is_none_when_the_split_factor_is_unknown() -> None:
    assert (
        market_cap(
            shares=1.0,
            shares_period_end=date(2024, 3, 31),
            price_on_signal_date=1.0,
            factor_at_period_end=None,
            factor_at_signal_date=1.0,
        )
        is None
    )


def test_momentum_skips_the_most_recent_month() -> None:
    """A spike confined to the skipped month must not register."""
    flat = [100.0] * 254
    assert momentum_12_1(flat) == pytest.approx(0.0)

    spiked = [100.0] * 254
    for i in range(-10, 0):
        spiked[i] = 500.0
    assert momentum_12_1(spiked) == pytest.approx(0.0)


def test_momentum_measures_the_twelve_to_one_month_window() -> None:
    prices = [100.0] * 254
    for i in range(-22, 0):
        prices[i] = 150.0
    # The window ends at -22, which is still the 150 era's first day.
    assert momentum_12_1(prices) == pytest.approx(0.5)


def test_momentum_needs_a_full_year_of_history() -> None:
    assert momentum_12_1([100.0] * 100) is None


def _annual_q1_series(values: list[float]) -> list:
    """One Q1 per year, 2019 onward - enough history for a surprise."""
    return quarterly_series(
        [
            _fact(date(2019 + i, 1, 1), date(2019 + i, 3, 31), v, i)
            for i, v in enumerate(values)
        ]
    )


def test_earnings_surprise_is_the_yoy_change_over_its_own_volatility() -> None:
    quarters = _annual_q1_series([100.0, 105.0, 120.0, 130.0, 150.0, 155.0, 205.0])
    result = earnings_surprise(quarters, date(2025, 3, 31), min_history=4, max_history=8)
    # Latest YoY change is +50; the prior five are [5, 20, 10, 15, 5], whose
    # population stdev is sqrt(34).
    assert result == pytest.approx(50.0 / 34.0**0.5)


def test_the_same_beat_is_a_bigger_surprise_at_a_steadier_firm() -> None:
    steady = _annual_q1_series([100.0, 110.0, 119.0, 130.0, 139.0, 150.0, 200.0])
    erratic = _annual_q1_series([100.0, 150.0, 90.0, 180.0, 70.0, 150.0, 200.0])
    steady_sue = earnings_surprise(steady, date(2025, 3, 31))
    erratic_sue = earnings_surprise(erratic, date(2025, 3, 31))
    assert steady_sue is not None and erratic_sue is not None
    assert steady_sue > erratic_sue


def test_earnings_surprise_is_undefined_when_the_firm_never_varies() -> None:
    """A zero spread would divide by zero, not signal an infinite surprise."""
    quarters = _annual_q1_series([100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0])
    assert earnings_surprise(quarters, date(2025, 3, 31)) is None


def test_earnings_surprise_returns_none_without_enough_history() -> None:
    facts = [
        _fact(date(2023, 1, 1), date(2023, 3, 31), 10.0, 1),
        _fact(date(2024, 1, 1), date(2024, 3, 31), 20.0, 2),
    ]
    quarters = quarterly_series(facts)
    assert earnings_surprise(quarters, date(2024, 3, 31)) is None


def test_low_accruals_is_negated_so_higher_is_better() -> None:
    """Earnings far above cash flow is the *bad* end, so it must score lower."""
    cash_backed = low_accruals(100.0, 100.0, 1000.0)
    accrual_heavy = low_accruals(100.0, 20.0, 1000.0)
    assert cash_backed is not None and accrual_heavy is not None
    assert cash_backed > accrual_heavy
    assert accrual_heavy == pytest.approx(-0.08)


def test_return_on_assets_rejects_non_positive_assets() -> None:
    assert return_on_assets(10.0, 0.0) is None
    assert return_on_assets(10.0, 100.0) == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def test_winsorize_clips_an_extreme_without_discarding_the_name() -> None:
    """BRK.B's net income swings by tens of billions with its equity book.

    Clipping keeps the name in the ranking at the top of the band instead of
    letting one print define the whole cross-section's scale.
    """
    values = {f"T{i:02d}": float(i) for i in range(49)}
    values["OUTLIER"] = 10_000.0
    clipped = winsorize(values, 5.0)
    assert len(clipped) == len(values)
    assert clipped["OUTLIER"] < 100.0
    assert clipped["OUTLIER"] == max(clipped.values())  # still ranked highest


def test_zscore_is_clipped_and_centred() -> None:
    values = {"A": 1.0, "B": 2.0, "C": 3.0, "D": 1000.0}
    zs = zscore(values, clip=3.0)
    assert max(zs.values()) <= 3.0
    assert min(zs.values()) >= -3.0


def test_zscore_of_a_flat_signal_is_flat_not_undefined() -> None:
    assert zscore({"A": 5.0, "B": 5.0}, clip=3.0) == {"A": 0.0, "B": 0.0}


def test_a_signal_held_by_too_few_names_is_dropped_for_that_date() -> None:
    signals = {
        "A": {"earnings_yield": 0.1, "revenue_growth": 0.2, "return_on_assets": 0.3},
        "B": {"earnings_yield": 0.2, "revenue_growth": 0.1, "return_on_assets": 0.2},
        "C": {"earnings_yield": 0.3, "revenue_growth": 0.3, "return_on_assets": 0.1},
        "D": {"earnings_yield": 0.4, "revenue_growth": 0.4, "return_on_assets": 0.4},
        "E": {"earnings_yield": 0.5, "revenue_growth": 0.5, "return_on_assets": 0.5},
        "F": {"earnings_yield": 0.6, "revenue_growth": 0.6, "return_on_assets": 0.6},
        # only two names carry momentum
        "G": {"earnings_yield": 0.7, "revenue_growth": 0.7, "return_on_assets": 0.7,
              "momentum_12_1": 1.0},
        "H": {"earnings_yield": 0.8, "revenue_growth": 0.8, "return_on_assets": 0.8,
              "momentum_12_1": 2.0},
    }
    scored = score_date(signals, {}, _scoring_config(min_tickers_per_signal=3))
    assert "momentum_12_1" in scored.dropped_signals
    assert "earnings_yield" not in scored.dropped_signals


def test_a_ticker_without_enough_groups_is_not_ranked() -> None:
    signals = {
        f"T{i}": {"earnings_yield": float(i), "revenue_growth": float(i),
                  "return_on_assets": float(i)}
        for i in range(5)
    }
    signals["THIN"] = {"earnings_yield": 9.0}  # value group only
    scored = score_date(signals, {}, _scoring_config())
    assert "THIN" not in scored.composites
    assert "T1" in scored.composites


def test_sector_neutralize_removes_the_sector_mean() -> None:
    composites = {"A": 3.0, "B": 1.0, "C": 2.0, "X": 10.0, "Y": 20.0, "Z": 30.0}
    sectors = {"A": "tech", "B": "tech", "C": "tech", "X": "energy", "Y": "energy", "Z": "energy"}
    out = sector_neutralize(composites, sectors, min_sector_size=3)
    assert sum(out[t] for t in ("A", "B", "C")) == pytest.approx(0.0)
    assert sum(out[t] for t in ("X", "Y", "Z")) == pytest.approx(0.0)
    # ...and the within-sector ordering survives
    assert out["A"] > out["C"] > out["B"]


def test_sector_neutralize_leaves_a_sector_too_small_to_average_alone() -> None:
    composites = {"A": 3.0, "B": 1.0, "SOLO": 99.0}
    sectors = {"A": "tech", "B": "tech", "SOLO": "materials"}
    out = sector_neutralize(composites, sectors, min_sector_size=3)
    assert out["SOLO"] == 99.0


def test_spearman_is_rank_based_not_level_based() -> None:
    xs = [1.0, 2.0, 3.0, 4.0]
    assert spearman(xs, [10.0, 20.0, 30.0, 40.0]) == pytest.approx(1.0)
    assert spearman(xs, [10.0, 20.0, 30.0, 4000.0]) == pytest.approx(1.0)
    assert spearman(xs, [40.0, 30.0, 20.0, 10.0]) == pytest.approx(-1.0)


def test_spearman_needs_at_least_three_points() -> None:
    assert spearman([1.0, 2.0], [1.0, 2.0]) is None


# ---------------------------------------------------------------------------
# book construction
# ---------------------------------------------------------------------------


def test_buffer_holds_a_name_that_slipped_out_of_the_entry_band() -> None:
    """A name at rank 12 is held, not traded away - scores are noisy."""
    composites = {f"T{i:02d}": float(20 - i) for i in range(20)}
    previous = ("T11",)  # rank 12
    book = select_book(composites, previous, entry_rank=10, hold_rank=15, short_side=False)
    assert "T11" in book
    assert len(book) == 10


def test_buffer_releases_a_name_that_fell_past_the_hold_band() -> None:
    composites = {f"T{i:02d}": float(20 - i) for i in range(20)}
    book = select_book(composites, ("T17",), entry_rank=10, hold_rank=15, short_side=False)
    assert "T17" not in book


def test_short_side_selects_from_the_bottom() -> None:
    composites = {f"T{i:02d}": float(20 - i) for i in range(20)}
    book = select_book(composites, (), entry_rank=3, hold_rank=5, short_side=True)
    assert set(book) == {"T19", "T18", "T17"}


def test_weight_cap_is_water_filled_not_merely_clipped() -> None:
    """Clipping alone leaves the weights summing to less than one."""
    capped = _apply_cap({"A": 0.6, "B": 0.3, "C": 0.1}, cap=0.4)
    assert sum(capped.values()) == pytest.approx(1.0)
    assert max(capped.values()) <= 0.4 + 1e-9


def test_weight_cap_below_equal_weight_falls_back_to_equal_weight() -> None:
    capped = _apply_cap({"A": 0.5, "B": 0.5}, cap=0.1)
    assert capped == {"A": 0.5, "B": 0.5}


def _panel(series: dict[str, dict[date, float]]) -> PricePanel:
    sessions = sorted({d for s in series.values() for d in s})
    return PricePanel(
        sessions=sessions, adj_close=series, raw_close=series, split_close=series
    )


def test_inverse_volatility_gives_the_calmer_name_more_weight() -> None:
    sessions = [date(2024, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(80)]
    calm = {d: 100.0 + i * 0.01 for i, d in enumerate(sessions)}
    wild = {d: 100.0 * (1.5 if i % 2 else 0.6) for i, d in enumerate(sessions)}
    panel = _panel({"CALM": calm, "WILD": wild})

    weights = inverse_volatility_weights(
        ("CALM", "WILD"), panel, sessions[-1], lookback=63, cap=0.9
    )
    assert weights["CALM"] > weights["WILD"]
    assert sum(weights.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# simulation
# ---------------------------------------------------------------------------


def _two_name_panel() -> PricePanel:
    import datetime as _dt

    sessions = [date(2024, 1, 1) + _dt.timedelta(days=i) for i in range(10)]
    up = {d: 100.0 * (1.01**i) for i, d in enumerate(sessions)}
    flat = {d: 100.0 for d in sessions}
    return _panel({"UP": up, "FLAT": flat})


def _config(**overrides: object) -> MultifactorConfig:
    defaults: dict[str, object] = {
        "rebalance": "monthly",
        "mode": "long_short",
        "entry_rank": 1,
        "hold_rank": 2,
        "min_scored": 1,
        "scoring": _scoring_config(),
        "vol_lookback_days": 5,
        "max_weight": 1.0,
        "execution_lag_sessions": 1,
        "cost_bps_per_dollar": 0.0,
        "borrow_bps_per_year": 0.0,
        "trading_days_per_year": 252,
        "max_days_since_period_end": 200,
        "exclusions": (),
        "surprise_min_history": 4,
        "surprise_max_history": 8,
    }
    defaults.update(overrides)
    return MultifactorConfig(**defaults)  # type: ignore[arg-type]


def _rebalance(panel: PricePanel, weights: dict[str, float]) -> Rebalance:
    return Rebalance(
        signal_date=panel.sessions[0],
        trade_date=panel.sessions[1],
        long=tuple(t for t, w in weights.items() if w > 0),
        short=tuple(t for t, w in weights.items() if w < 0),
        weights=weights,
        scored=score_date({}, {}, _scoring_config()),
        signals={},
    )


def test_a_long_position_in_a_rising_name_gains() -> None:
    panel = _two_name_panel()
    curve = simulate([_rebalance(panel, {"UP": 1.0})], panel, _config(), frictions=True)
    assert curve[-1][1] > curve[0][1]


def test_a_dollar_neutral_book_is_flat_when_both_legs_move_together() -> None:
    import datetime as _dt

    sessions = [date(2024, 1, 1) + _dt.timedelta(days=i) for i in range(10)]
    both = {d: 100.0 * (1.02**i) for i, d in enumerate(sessions)}
    panel = _panel({"A": dict(both), "B": dict(both)})
    curve = simulate(
        [_rebalance(panel, {"A": 1.0, "B": -1.0})], panel, _config(), frictions=True
    )
    assert curve[-1][1] == pytest.approx(1.0, abs=1e-9)


def test_trading_costs_reduce_nav_relative_to_gross() -> None:
    panel = _two_name_panel()
    rebalances = [_rebalance(panel, {"UP": 1.0, "FLAT": -1.0})]
    net = simulate(rebalances, panel, _config(cost_bps_per_dollar=10.0), frictions=True)
    gross = simulate(rebalances, panel, _config(cost_bps_per_dollar=10.0), frictions=False)
    assert net[-1][1] < gross[-1][1]


def test_borrow_is_charged_on_the_short_leg_only() -> None:
    panel = _two_name_panel()
    long_only = simulate(
        [_rebalance(panel, {"FLAT": 1.0})], panel, _config(borrow_bps_per_year=5000.0),
        frictions=True,
    )
    short_only = simulate(
        [_rebalance(panel, {"FLAT": -1.0})], panel, _config(borrow_bps_per_year=5000.0),
        frictions=True,
    )
    assert long_only[-1][1] == pytest.approx(1.0, abs=1e-9)
    assert short_only[-1][1] < 1.0


def test_positions_drift_between_rebalances_rather_than_resetting() -> None:
    """Two days of compounding, not two days of the initial weight."""
    panel = _two_name_panel()
    curve = simulate([_rebalance(panel, {"UP": 1.0})], panel, _config(), frictions=False)
    prices = panel.adj_close["UP"]
    entry = prices[panel.sessions[1]]  # traded at the close after the signal
    assert curve[0][0] == panel.sessions[1]
    assert curve[-1][1] == pytest.approx(prices[panel.sessions[-1]] / entry)


def test_summarize_reports_a_real_drawdown() -> None:
    curve = [
        (date(2024, 1, 1), 1.0),
        (date(2024, 1, 2), 1.5),
        (date(2024, 1, 3), 0.75),
        (date(2024, 1, 4), 1.2),
    ]
    summary = summarize(curve, [])
    assert summary.max_drawdown == pytest.approx(-0.5)
    assert summary.cumulative_return == pytest.approx(0.2)


def test_rebalance_sessions_picks_the_last_real_session_of_each_month() -> None:
    sessions = [
        date(2024, 1, 30), date(2024, 1, 31),
        date(2024, 2, 28), date(2024, 2, 29),
        date(2024, 3, 27),  # a month whose last session isn't the 31st
    ]
    monthly = rebalance_sessions(sessions, "monthly", date(2024, 1, 1), date(2024, 12, 31))
    assert monthly == [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 27)]

    quarterly = rebalance_sessions(sessions, "quarterly", date(2024, 1, 1), date(2024, 12, 31))
    assert quarterly == [date(2024, 3, 27)]


def test_rebalance_cadence_must_be_recognised() -> None:
    with pytest.raises(ValueError, match="monthly"):
        rebalance_sessions([date(2024, 1, 31)], "weekly", date(2024, 1, 1), date(2024, 12, 31))


# ---------------------------------------------------------------------------
# fundamentals views
# ---------------------------------------------------------------------------


def _facts_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "ticker": pl.Utf8,
            "metric_code": pl.Utf8,
            "period_start": pl.Date,
            "period_end": pl.Date,
            "value": pl.Float64,
        },
    )


def _row(metric: str, end: date, value: float, ticker: str = "A") -> dict[str, object]:
    return {
        "ticker": ticker,
        "metric_code": metric,
        "period_start": date(end.year, end.month, 1),
        "period_end": end,
        "value": value,
    }


def test_restated_view_revalues_known_periods_without_adding_unfiled_ones() -> None:
    pit = _facts_frame([_row("net_income", date(2024, 3, 31), 100.0)])
    latest = _facts_frame(
        [
            _row("net_income", date(2024, 3, 31), 130.0),  # restated
            _row("net_income", date(2024, 6, 30), 200.0),  # not yet filed at the cutoff
        ]
    )
    out = make_fundamentals_view(
        FundamentalsView.RESTATED_KNOWN_PERIODS, pit, latest, date(2024, 7, 15)
    )
    assert out["period_end"].to_list() == [date(2024, 3, 31)]
    assert out["value"].to_list() == [130.0]


def test_naive_view_admits_a_quarter_that_had_ended_but_not_been_filed() -> None:
    pit = _facts_frame([_row("net_income", date(2024, 3, 31), 100.0)])
    latest = _facts_frame(
        [
            _row("net_income", date(2024, 3, 31), 130.0),
            _row("net_income", date(2024, 6, 30), 200.0),
        ]
    )
    out = make_fundamentals_view(
        FundamentalsView.LATEST_NAIVE, pit, latest, date(2024, 7, 15)
    )
    assert sorted(out["period_end"].to_list()) == [date(2024, 3, 31), date(2024, 6, 30)]


def test_naive_view_still_excludes_a_quarter_that_had_not_ended() -> None:
    pit = _facts_frame([_row("net_income", date(2024, 3, 31), 100.0)])
    latest = _facts_frame(
        [
            _row("net_income", date(2024, 3, 31), 130.0),
            _row("net_income", date(2024, 9, 30), 300.0),
        ]
    )
    out = make_fundamentals_view(
        FundamentalsView.LATEST_NAIVE, pit, latest, date(2024, 7, 15)
    )
    assert out["period_end"].to_list() == [date(2024, 3, 31)]


def test_share_counts_stay_point_in_time_in_every_view() -> None:
    """A later filing restates share counts onto a post-split basis.

    Letting a "restated" run see that would move market cap mechanically
    rather than economically, which is not the effect being measured.
    """
    pit = _facts_frame([_row("shares_outstanding_diluted", date(2024, 3, 31), 1_000.0)])
    latest = _facts_frame(
        [_row("shares_outstanding_diluted", date(2024, 3, 31), 10_000.0)]  # post 10:1 split
    )
    for view in (FundamentalsView.RESTATED_KNOWN_PERIODS, FundamentalsView.LATEST_NAIVE):
        out = make_fundamentals_view(view, pit, latest, date(2024, 7, 15))
        assert out["value"].to_list() == [1_000.0], view


def test_point_in_time_view_is_returned_unchanged() -> None:
    pit = _facts_frame([_row("net_income", date(2024, 3, 31), 100.0)])
    latest = _facts_frame([_row("net_income", date(2024, 3, 31), 999.0)])
    out = make_fundamentals_view(FundamentalsView.POINT_IN_TIME, pit, latest, date(2024, 7, 15))
    assert out["value"].to_list() == [100.0]


# ---------------------------------------------------------------------------
# report plumbing
# ---------------------------------------------------------------------------


MARKER = "<!-- pdw:strategy-report -->"


def test_upsert_section_preserves_the_original_report_above_the_marker() -> None:
    existing = "# Findings\n\nOriginal backtest content.\n"
    out = upsert_section(existing, "# Strategy\n\nNew.", MARKER)
    assert out.startswith("# Findings")
    assert "Original backtest content." in out
    assert out.count(MARKER) == 1
    assert out.rstrip().endswith("New.")


def test_upsert_section_replaces_a_previous_strategy_section() -> None:
    existing = f"# Findings\n\nKeep me.\n\n{MARKER}\n\n# Strategy\n\nStale numbers.\n"
    out = upsert_section(existing, "# Strategy\n\nFresh numbers.", MARKER)
    assert "Stale numbers." not in out
    assert "Fresh numbers." in out
    assert "Keep me." in out
    assert out.count(MARKER) == 1


def test_strip_section_round_trips_with_upsert() -> None:
    """`pdw backtest run` regenerates its half and must not drop the other."""
    combined = upsert_section("# Findings\n\nBacktest.\n", "# Strategy\n\nBody.", MARKER)
    section = strip_section(combined, MARKER)
    assert section.startswith(MARKER)
    assert "Body." in section
    assert "Backtest." not in section


def test_strip_section_is_empty_when_no_strategy_has_run() -> None:
    assert strip_section("# Findings\n\nJust the backtest.\n", MARKER) == ""


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_exclusion_applies_only_to_its_own_tickers() -> None:
    config = _config(
        exclusions=(
            Exclusion(
                tickers=frozenset({"JPM"}),
                signals=frozenset({"revenue_growth"}),
                reason="banks",
            ),
        )
    )
    assert config.excluded_signals("JPM") == frozenset({"revenue_growth"})
    assert config.excluded_signals("AAPL") == frozenset()


def test_most_recent_book_lists_both_legs_with_signed_weights() -> None:
    panel = _two_name_panel()
    rebalance = _rebalance(panel, {"UP": 0.6, "FLAT": -0.4})
    table = "\n".join(latest_rebalance_detail(rebalance))
    assert "| long | UP | +60.00%" in table
    assert "| short | FLAT | -40.00%" in table
