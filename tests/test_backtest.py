from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from pdw.backtest import (
    BacktestRun,
    EarningsYieldInput,
    FactRef,
    Portfolio,
    _ttm_net_income,
    build_portfolio,
    compare_portfolios,
    compute_earnings_yields,
    compute_forward_return,
    equity_curve,
    find_case_studies,
    generate_rebalance_dates,
    run_backtest,
    summarize,
)


def test_generate_rebalance_dates_is_quarterly_calendar_aligned() -> None:
    dates = generate_rebalance_dates(date(2023, 2, 15), date(2024, 1, 1))

    assert dates == [date(2023, 4, 1), date(2023, 7, 1), date(2023, 10, 1), date(2024, 1, 1)]


def _candidate(ticker: str, earnings_yield: float) -> EarningsYieldInput:
    fact = FactRef(fact_id=1, accession_no="acc", period_end=date(2023, 3, 31), value=100.0)
    return EarningsYieldInput(
        ticker=ticker,
        earnings_yield=earnings_yield,
        ttm_net_income=400.0,
        net_income_facts=(fact,),
        shares_fact=fact,
        price_date=date(2023, 4, 3),
        price=10.0,
        market_cap=1000.0,
    )


def test_build_portfolio_longs_the_highest_yield_and_shorts_the_lowest() -> None:
    candidates = {f"T{i}": _candidate(f"T{i}", float(i)) for i in range(25)}

    portfolio = build_portfolio(date(2023, 4, 1), candidates)

    assert portfolio is not None
    assert set(portfolio.long) == {f"T{i}" for i in range(15, 25)}
    assert set(portfolio.short) == {f"T{i}" for i in range(0, 10)}


def test_build_portfolio_returns_none_when_too_few_candidates() -> None:
    candidates = {f"T{i}": _candidate(f"T{i}", float(i)) for i in range(5)}

    assert build_portfolio(date(2023, 4, 1), candidates) is None


def _portfolio(rebalance_date: date, long: tuple[str, ...], short: tuple[str, ...]) -> Portfolio:
    candidates = {t: _candidate(t, 1.0) for t in long + short}
    return Portfolio(rebalance_date=rebalance_date, long=long, short=short, candidates=candidates)


def test_summarize_computes_cumulative_return_sharpe_and_turnover() -> None:
    from pdw.backtest import PeriodReturn

    run = BacktestRun(mode="point_in_time")
    run.portfolios = [
        _portfolio(date(2023, 1, 1), ("A", "B"), ("C", "D")),
        _portfolio(date(2023, 4, 1), ("A", "E"), ("C", "D")),  # B->E: 1 of 4 slots changed
    ]
    run.period_returns = [
        PeriodReturn(date(2023, 1, 1), date(2023, 4, 1), long_return=0.10, short_return=0.02),
    ]

    summary = summarize(run)

    assert summary.cumulative_return == pytest.approx(0.08)
    assert summary.avg_turnover == pytest.approx(0.25)
    assert summary.n_periods == 1


def test_equity_curve_starts_at_one_and_compounds() -> None:
    from pdw.backtest import PeriodReturn

    run = BacktestRun(mode="point_in_time")
    run.portfolios = [
        _portfolio(date(2023, 1, 1), ("A",), ("B",)),
        _portfolio(date(2023, 4, 1), ("A",), ("B",)),
        _portfolio(date(2023, 7, 1), ("A",), ("B",)),
    ]
    run.period_returns = [
        PeriodReturn(date(2023, 1, 1), date(2023, 4, 1), long_return=0.10, short_return=0.0),
        PeriodReturn(date(2023, 4, 1), date(2023, 7, 1), long_return=-0.10, short_return=0.0),
    ]

    curve = equity_curve(run)

    assert curve[0] == (date(2023, 1, 1), 1.0)
    assert curve[1][1] == pytest.approx(1.10)
    assert curve[2][1] == pytest.approx(1.10 * 0.90)


def test_compare_portfolios_finds_only_the_differing_ticker() -> None:
    pit_run = BacktestRun(mode="point_in_time")
    pit_run.portfolios = [_portfolio(date(2023, 4, 1), ("A", "B"), ("C", "D"))]
    latest_run = BacktestRun(mode="latest")
    latest_run.portfolios = [_portfolio(date(2023, 4, 1), ("A", "E"), ("C", "D"))]

    differences = compare_portfolios(pit_run, latest_run)

    tickers = {d.ticker for d in differences}
    assert tickers == {"B", "E"}
    b_diff = next(d for d in differences if d.ticker == "B")
    assert b_diff.point_in_time_side == "long"
    assert b_diff.latest_side is None
    e_diff = next(d for d in differences if d.ticker == "E")
    assert e_diff.point_in_time_side is None
    assert e_diff.latest_side == "long"


def test_compare_portfolios_ignores_dates_only_one_run_has() -> None:
    pit_run = BacktestRun(mode="point_in_time")
    pit_run.portfolios = [_portfolio(date(2023, 4, 1), ("A",), ("B",))]
    latest_run = BacktestRun(mode="latest")
    latest_run.portfolios = []

    assert compare_portfolios(pit_run, latest_run) == []


def test_find_case_studies_traces_the_changed_net_income_fact() -> None:
    rebalance_date = date(2023, 4, 1)
    period_end = date(2022, 12, 31)
    pit_fact = FactRef(
        fact_id=100, accession_no="acc-original", period_end=period_end, value=50.0
    )
    latest_fact = FactRef(
        fact_id=200, accession_no="acc-amended", period_end=period_end, value=80.0
    )

    def _input_with_fact(fact: FactRef) -> EarningsYieldInput:
        return EarningsYieldInput(
            ticker="ACME",
            earnings_yield=0.05,
            ttm_net_income=fact.value,
            net_income_facts=(fact,),
            shares_fact=fact,
            price_date=rebalance_date,
            price=10.0,
            market_cap=1000.0,
        )

    pit_portfolio = Portfolio(
        rebalance_date=rebalance_date,
        long=("ACME",),
        short=(),
        candidates={"ACME": _input_with_fact(pit_fact)},
    )
    latest_portfolio = Portfolio(
        rebalance_date=rebalance_date,
        long=(),
        short=("ACME",),
        candidates={"ACME": _input_with_fact(latest_fact)},
    )
    pit_run = BacktestRun(mode="point_in_time", portfolios=[pit_portfolio])
    latest_run = BacktestRun(mode="latest", portfolios=[latest_portfolio])
    differences = compare_portfolios(pit_run, latest_run)

    case_studies = find_case_studies(differences, pit_run, latest_run)

    assert len(case_studies) == 1
    cs = case_studies[0]
    assert cs.ticker == "ACME"
    assert cs.point_in_time_fact_id == 100
    assert cs.point_in_time_accession_no == "acc-original"
    assert cs.point_in_time_value == 50.0
    assert cs.latest_fact_id == 200
    assert cs.latest_accession_no == "acc-amended"
    assert cs.latest_value == 80.0
    assert cs.point_in_time_side == "long"
    assert cs.latest_side == "short"


# --- DB-backed: compute_earnings_yields / run_backtest end-to-end ----------


def _make_entity_with_ticker(conn: psycopg.Connection, cik: str, ticker: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.entity (cik, name) VALUES (%s, 'Test Corp') RETURNING entity_id",
            (cik,),
        )
        row = cur.fetchone()
        assert row is not None
        entity_id: int = row[0]
        cur.execute(
            "INSERT INTO core.entity_ticker (entity_id, ticker, knowledge_from) "
            "VALUES (%s, %s, %s)",
            (entity_id, ticker, datetime(2000, 1, 1, tzinfo=UTC)),
        )
    conn.commit()
    return entity_id


def _make_payload(conn: psycopg.Connection, source: str) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ops.pipeline_run (pipeline) VALUES ('test') RETURNING run_id")
        row = cur.fetchone()
        assert row is not None
        run_id = row[0]
        cur.execute(
            """
            INSERT INTO raw.payload
                (source, endpoint, request_params, fetched_at, http_status,
                 content_sha256, body, run_id)
            VALUES (%s, 'x', '{}'::jsonb, now(), 200, repeat('0', 64), 'x', %s)
            RETURNING payload_id
            """,
            (source, run_id),
        )
        row = cur.fetchone()
        assert row is not None
        payload_id: int = row[0]
    conn.commit()
    return payload_id


def _insert_net_income(
    conn: psycopg.Connection,
    *,
    entity_id: int,
    payload_id: int,
    period_end: date,
    value: float,
    knowledge_from: datetime,
    knowledge_to: str = "infinity",
    accession_no: str = "acc",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.fundamental_fact
                (entity_id, metric_code, period_start, period_end, value, unit, source,
                 accession_no, filed_date, knowledge_from, knowledge_to, payload_id)
            VALUES (%s, 'net_income', %s, %s, %s, 'USD', 'edgar', %s, %s, %s, %s::timestamptz, %s)
            """,
            (
                entity_id,
                period_end - timedelta(days=90),
                period_end,
                value,
                accession_no,
                knowledge_from.date(),
                knowledge_from,
                knowledge_to,
                payload_id,
            ),
        )
    conn.commit()


def _insert_shares(
    conn: psycopg.Connection, *, entity_id: int, payload_id: int, period_end: date, value: float
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.fundamental_fact
                (entity_id, metric_code, period_end, value, unit, source,
                 filed_date, knowledge_from, payload_id)
            VALUES (%s, 'shares_outstanding_diluted', %s, %s, 'shares', 'edgar', %s, %s, %s)
            """,
            (entity_id, period_end, value, period_end, datetime.combine(
                period_end, datetime.min.time(), tzinfo=UTC
            ), payload_id),
        )
    conn.commit()


def _insert_price(
    conn: psycopg.Connection,
    *,
    entity_id: int,
    payload_id: int,
    trade_date: date,
    close: float,
    adj_close: float,
    source: str,
) -> None:
    knowledge_from = datetime.combine(trade_date, datetime.min.time(), tzinfo=UTC)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.price_fact
                (entity_id, trade_date, close, adj_close, source, knowledge_from, payload_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (entity_id, trade_date, close, adj_close, source, knowledge_from, payload_id),
        )
    conn.commit()


def test_compute_earnings_yields_end_to_end(db_connection: psycopg.Connection) -> None:
    entity_id = _make_entity_with_ticker(db_connection, "7777777701", "TTEST")
    edgar_payload = _make_payload(db_connection, "edgar")
    tiingo_payload = _make_payload(db_connection, "tiingo")
    yf_payload = _make_payload(db_connection, "yfinance")
    rebalance_date = date(2023, 4, 3)

    quarter_ends = [date(2022, 6, 30), date(2022, 9, 30), date(2022, 12, 31), date(2023, 3, 31)]
    for period_end in quarter_ends:
        _insert_net_income(
            db_connection,
            entity_id=entity_id,
            payload_id=edgar_payload,
            period_end=period_end,
            value=25.0,
            knowledge_from=datetime.combine(period_end, datetime.min.time(), tzinfo=UTC),
        )
    _insert_shares(
        db_connection, entity_id=entity_id, payload_id=edgar_payload,
        period_end=date(2023, 3, 31), value=10.0,
    )
    _insert_price(
        db_connection, entity_id=entity_id, payload_id=tiingo_payload,
        trade_date=rebalance_date, close=100.0, adj_close=100.0, source="tiingo",
    )
    _insert_price(
        db_connection, entity_id=entity_id, payload_id=yf_payload,
        trade_date=rebalance_date, close=100.0, adj_close=100.0, source="yfinance",
    )

    as_of = datetime.combine(rebalance_date, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
    result = compute_earnings_yields(db_connection, as_of, as_of, ["TTEST"], rebalance_date)

    assert "TTEST" in result
    inp = result["TTEST"]
    assert inp.ttm_net_income == 100.0  # 4 quarters of 25
    assert inp.market_cap == 1000.0  # 10 shares * 100 price
    assert inp.earnings_yield == 0.1


def test_compute_earnings_yields_excludes_ytd_cumulative_fact(
    db_connection: psycopg.Connection,
) -> None:
    """The same M4/M6 EDGAR shape - a YTD-cumulative fact sharing the same
    metric_code must not be double-counted or substituted for a genuine
    single quarter when computing TTM net income."""
    entity_id = _make_entity_with_ticker(db_connection, "7777777702", "TYTD")
    edgar_payload = _make_payload(db_connection, "edgar")
    rebalance_date = date(2023, 4, 3)

    quarter_ends = [date(2022, 6, 30), date(2022, 9, 30), date(2022, 12, 31), date(2023, 3, 31)]
    for period_end in quarter_ends:
        _insert_net_income(
            db_connection, entity_id=entity_id, payload_id=edgar_payload,
            period_end=period_end, value=25.0,
            knowledge_from=datetime.combine(period_end, datetime.min.time(), tzinfo=UTC),
        )
    # 9-month YTD figure as of the Q3 period_end - must not enter the TTM sum.
    with db_connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.fundamental_fact
                (entity_id, metric_code, period_start, period_end, value, unit, source,
                 filed_date, knowledge_from, payload_id)
            VALUES (%s, 'net_income', %s, %s, %s, 'USD', 'edgar', %s, %s, %s)
            """,
            (
                entity_id, date(2022, 1, 1), date(2022, 9, 30), 75.0,
                date(2022, 9, 30),
                datetime(2022, 9, 30, tzinfo=UTC),
                edgar_payload,
            ),
        )
    db_connection.commit()

    as_of = datetime.combine(rebalance_date, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)

    result = _ttm_net_income(db_connection, as_of, "TYTD", rebalance_date)

    assert result is not None
    total, facts = result
    assert total == 100.0  # exactly the 4 genuine quarters, YTD row excluded
    assert len(facts) == 4


def test_ttm_net_income_uses_the_rebalance_periods_not_todays_most_recent(
    db_connection: psycopg.Connection,
) -> None:
    """Regression: found live against the full 50-ticker universe - a full
    comparison run showed ~90% of every rebalance's positions "differing",
    across all 39 rebalances spanning 2017-2026. Root cause: mode="latest"
    uses as_of=now() for every historical rebalance, and _ttm_net_income
    used to just take the 4 most-recent-as-of-`as_of` quarters, regardless
    of which rebalance was being evaluated - for a 2017 rebalance, "latest"
    mode was silently using 2026's quarters instead of 2016's *restated*
    values, comparing entirely different calendar periods rather than
    isolating the effect of restatement on the same periods.
    """
    entity_id = _make_entity_with_ticker(db_connection, "7777777706", "TOLD")
    edgar_payload = _make_payload(db_connection, "edgar")
    old_rebalance_date = date(2016, 4, 3)

    for quarter_start in range(4):
        period_end = date(2015, 3, 31) + timedelta(days=91 * quarter_start)
        _insert_net_income(
            db_connection, entity_id=entity_id, payload_id=edgar_payload,
            period_end=period_end, value=10.0,
            knowledge_from=datetime.combine(period_end, datetime.min.time(), tzinfo=UTC),
        )
    # Much more recent quarters, unrelated to the 2016 rebalance - "latest"
    # mode must not pick these up when evaluating the 2016 date.
    for quarter_start in range(4):
        period_end = date(2025, 3, 31) + timedelta(days=91 * quarter_start)
        _insert_net_income(
            db_connection, entity_id=entity_id, payload_id=edgar_payload,
            period_end=period_end, value=999.0,
            knowledge_from=datetime.combine(period_end, datetime.min.time(), tzinfo=UTC),
        )

    result = _ttm_net_income(db_connection, datetime.now(UTC), "TOLD", old_rebalance_date)

    assert result is not None
    total, facts = result
    assert total == 40.0  # the 2016-relevant quarters, not the 999-valued 2025 ones
    assert all(f.period_end <= old_rebalance_date for f in facts)


def test_run_backtest_point_in_time_excludes_a_future_restatement(
    db_connection: psycopg.Connection,
) -> None:
    """A minimal end-to-end version of the M7 experiment itself: a
    restatement filed after the rebalance date must be invisible to the
    point_in_time run and visible to the latest run, changing the computed
    earnings yield between the two."""
    entity_id = _make_entity_with_ticker(db_connection, "7777777703", "TAMEND")
    edgar_payload = _make_payload(db_connection, "edgar")
    tiingo_payload = _make_payload(db_connection, "tiingo")
    yf_payload = _make_payload(db_connection, "yfinance")
    rebalance_date = date(2023, 4, 3)
    amended_period_end = date(2023, 3, 31)

    for period_end in [date(2022, 6, 30), date(2022, 9, 30), date(2022, 12, 31)]:
        _insert_net_income(
            db_connection, entity_id=entity_id, payload_id=edgar_payload,
            period_end=period_end, value=25.0,
            knowledge_from=datetime.combine(period_end, datetime.min.time(), tzinfo=UTC),
        )
    original_kf = datetime.combine(amended_period_end, datetime.min.time(), tzinfo=UTC)
    amended_kf = datetime(2023, 8, 1, tzinfo=UTC)  # filed well after the rebalance
    _insert_net_income(
        db_connection, entity_id=entity_id, payload_id=edgar_payload,
        period_end=amended_period_end, value=25.0,
        knowledge_from=original_kf, knowledge_to=str(amended_kf), accession_no="acc-original",
    )
    _insert_net_income(
        db_connection, entity_id=entity_id, payload_id=edgar_payload,
        period_end=amended_period_end, value=65.0,
        knowledge_from=amended_kf, accession_no="acc-amended",
    )
    _insert_shares(
        db_connection, entity_id=entity_id, payload_id=edgar_payload,
        period_end=amended_period_end, value=10.0,
    )
    for source, payload_id in [("tiingo", tiingo_payload), ("yfinance", yf_payload)]:
        _insert_price(
            db_connection, entity_id=entity_id, payload_id=payload_id,
            trade_date=rebalance_date, close=100.0, adj_close=100.0, source=source,
        )

    pit_as_of = datetime.combine(rebalance_date, datetime.min.time(), tzinfo=UTC) + timedelta(
        days=1
    )
    latest_as_of = datetime(2023, 9, 1, tzinfo=UTC)

    pit_result = compute_earnings_yields(
        db_connection, pit_as_of, pit_as_of, ["TAMEND"], rebalance_date
    )
    latest_result = compute_earnings_yields(
        db_connection, latest_as_of, latest_as_of, ["TAMEND"], rebalance_date
    )

    assert pit_result["TAMEND"].ttm_net_income == 100.0  # 25+25+25+25 (original)
    assert latest_result["TAMEND"].ttm_net_income == 140.0  # 25+25+25+65 (restated)


def test_run_backtest_end_to_end_with_a_small_synthetic_universe(
    db_connection: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises the full pipeline (compute_earnings_yields -> build_portfolio
    -> compute_forward_return -> summarize) wired together the way
    run_backtest does it, on a universe small enough to seed by hand -
    MIN_VALID_TICKERS/QUANTILE_SIZE are patched down to fit 4 tickers rather
    than the real 50-name universe's 20/10."""
    import pdw.backtest as backtest_module

    monkeypatch.setattr(backtest_module, "MIN_VALID_TICKERS", 4)
    monkeypatch.setattr(backtest_module, "QUANTILE_SIZE", 1)

    edgar_payload = _make_payload(db_connection, "edgar")
    tiingo_payload = _make_payload(db_connection, "tiingo")
    yf_payload = _make_payload(db_connection, "yfinance")
    rebalance_dates = [date(2023, 1, 2), date(2023, 4, 3)]

    # 4 tickers with increasing net income (and equal shares/price, so
    # ranking is driven purely by net income) - T4 should end up long,
    # T1 short, at both rebalances. 4 base quarters, all dated in 2022,
    # are visible well before either 2023 rebalance - no need to insert
    # anything rebalance-specific for net income or shares.
    for i, ticker in enumerate(["T1", "T2", "T3", "T4"], start=1):
        entity_id = _make_entity_with_ticker(db_connection, f"888888880{i}", ticker)
        for quarter_start in range(4):
            period_end = date(2022, 3, 31) + timedelta(days=91 * quarter_start)
            _insert_net_income(
                db_connection, entity_id=entity_id, payload_id=edgar_payload,
                period_end=period_end, value=float(i) * 10,
                knowledge_from=datetime.combine(period_end, datetime.min.time(), tzinfo=UTC),
            )
        _insert_shares(
            db_connection, entity_id=entity_id, payload_id=edgar_payload,
            period_end=date(2022, 12, 29), value=10.0,
        )
        for rebalance_date in rebalance_dates:
            for source, payload_id in [("tiingo", tiingo_payload), ("yfinance", yf_payload)]:
                _insert_price(
                    db_connection, entity_id=entity_id, payload_id=payload_id,
                    trade_date=rebalance_date, close=100.0, adj_close=100.0, source=source,
                )

    run = run_backtest(db_connection, ["T1", "T2", "T3", "T4"], rebalance_dates, "point_in_time")

    assert len(run.portfolios) == 2
    assert run.portfolios[0].long == ("T4",)
    assert run.portfolios[0].short == ("T1",)
    assert len(run.period_returns) == 1
    summary = summarize(run)
    assert summary.n_periods == 1
    assert summary.avg_turnover == 0.0  # same portfolio composition both times


def test_compute_forward_return_uses_yfinance_adj_close(db_connection: psycopg.Connection) -> None:
    entity_id = _make_entity_with_ticker(db_connection, "7777777704", "TRET")
    yf_payload = _make_payload(db_connection, "yfinance")
    start_date, end_date = date(2023, 4, 3), date(2023, 7, 3)
    _insert_price(
        db_connection, entity_id=entity_id, payload_id=yf_payload,
        trade_date=start_date, close=100.0, adj_close=100.0, source="yfinance",
    )
    _insert_price(
        db_connection, entity_id=entity_id, payload_id=yf_payload,
        trade_date=end_date, close=110.0, adj_close=110.0, source="yfinance",
    )
    as_of = datetime(2023, 8, 1, tzinfo=UTC)

    result = compute_forward_return(db_connection, as_of, "TRET", start_date, end_date)

    assert result == pytest.approx(0.10)


def test_compute_earnings_yields_finds_a_price_with_realistic_availability_lag(
    db_connection: psycopg.Connection,
) -> None:
    """Regression: found live against the full 50-ticker universe - a full
    run produced *zero* rebalances. The real loader sets a price's
    knowledge_from to trade_date + availability_lag (CLAUDE.md 1), not
    trade_date itself, so a fundamentals_as_of fixed at midnight of the
    rebalance date (correct for point-in-time fundamentals) can never see
    that same day's own price, or any later one - every rebalance's price
    lookup failed silently. price_as_of must stay "now" even when
    fundamentals_as_of is historical.
    """
    entity_id = _make_entity_with_ticker(db_connection, "7777777705", "TLAG")
    edgar_payload = _make_payload(db_connection, "edgar")
    tiingo_payload = _make_payload(db_connection, "tiingo")
    yf_payload = _make_payload(db_connection, "yfinance")
    rebalance_date = date(2023, 4, 3)

    for quarter_start in range(4):
        period_end = date(2022, 3, 31) + timedelta(days=91 * quarter_start)
        _insert_net_income(
            db_connection, entity_id=entity_id, payload_id=edgar_payload,
            period_end=period_end, value=25.0,
            knowledge_from=datetime.combine(period_end, datetime.min.time(), tzinfo=UTC),
        )
    _insert_shares(
        db_connection, entity_id=entity_id, payload_id=edgar_payload,
        period_end=date(2022, 12, 29), value=10.0,
    )
    # Realistic lag: knowledge_from is trade_date + 1 day, not trade_date
    # itself - matching pdw.availability.compute_knowledge_from, not the
    # other _insert_price calls in this file (which deliberately don't
    # model lag, which is why this bug slipped past every other test here).
    for source, payload_id in [("tiingo", tiingo_payload), ("yfinance", yf_payload)]:
        with db_connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO core.price_fact
                    (entity_id, trade_date, close, adj_close, source, knowledge_from, payload_id)
                VALUES (%s, %s, 100.0, 100.0, %s, %s, %s)
                """,
                (
                    entity_id,
                    rebalance_date,
                    source,
                    datetime.combine(rebalance_date, datetime.min.time(), tzinfo=UTC)
                    + timedelta(days=1),
                    payload_id,
                ),
            )
        db_connection.commit()

    fundamentals_as_of = datetime.combine(rebalance_date, datetime.min.time(), tzinfo=UTC)
    price_as_of = datetime.now(UTC)

    result = compute_earnings_yields(
        db_connection, fundamentals_as_of, price_as_of, ["TLAG"], rebalance_date
    )

    assert "TLAG" in result
