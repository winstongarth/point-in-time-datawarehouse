from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from pdw.dq_checks import (
    CheckResult,
    check_balance_sheet_identity,
    check_cross_vendor_price,
    check_payload_freshness,
    check_period_coverage_gaps,
    check_price_staleness,
    check_return_outliers,
    check_revenue_sanity,
    check_tag_switches,
)

_RULES = [
    {
        "name": "price_close_cross_vendor",
        "left": {"source": "yfinance", "field": "close"},
        "right": {"source": "tiingo", "field": "close"},
        "grain": ["entity_id", "trade_date"],
        "tolerance": {"type": "relative", "value": 0.001},
        "severity": "WARN",
        "escalate_if": {"consecutive_days": 3, "severity": "BREAK"},
    }
]


def _make_entity(conn: psycopg.Connection, cik: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.entity (cik, name) VALUES (%s, 'Test Corp') RETURNING entity_id",
            (cik,),
        )
        row = cur.fetchone()
        assert row is not None
        entity_id: int = row[0]
    conn.commit()
    return entity_id


def _make_payload(
    conn: psycopg.Connection,
    *,
    source: str = "edgar",
    endpoint: str = "companyfacts",
    request_params: dict[str, object] | None = None,
    body: bytes = b"x",
) -> int:
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
            VALUES (%s, %s, %s, now(), 200, repeat('0', 64), %s, %s)
            RETURNING payload_id
            """,
            (source, endpoint, Jsonb(request_params or {}), body, run_id),
        )
        row = cur.fetchone()
        assert row is not None
        payload_id: int = row[0]
    conn.commit()
    return payload_id


def _insert_price(
    conn: psycopg.Connection,
    *,
    entity_id: int,
    trade_date: date,
    close: float | None,
    source: str,
    payload_id: int,
    adj_close: float | None = None,
) -> None:
    knowledge_from = datetime.combine(trade_date, datetime.min.time(), tzinfo=UTC) + timedelta(
        days=1
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.price_fact (entity_id, trade_date, close, adj_close,
                                          source, knowledge_from, payload_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (entity_id, trade_date, close, adj_close, source, knowledge_from, payload_id),
        )
    conn.commit()


# period_start is NULL for instant concepts (Assets,
# StockholdersEquity, shares outstanding) and populated for duration ones
# (revenue, net_income, operating_cash_flow) - only the latter get an
# auto-computed quarterly default below.
_DURATION_METRICS = {"revenue", "net_income", "operating_cash_flow"}


def _insert_fundamental(
    conn: psycopg.Connection,
    *,
    entity_id: int,
    metric_code: str,
    period_end: date,
    value: float,
    payload_id: int,
    vendor_native_tag: str = "SomeTag",
    fiscal_period: str = "Q1",
    filed_date: date | None = None,
    period_start: date | None = None,
) -> None:
    filed_date = filed_date or period_end
    # Default to a genuine single-quarter duration (~90 days) unless a test
    # explicitly wants to model a YTD-cumulative or annual figure - real
    # EDGAR data mixes those under the same fiscal_period label, so tests
    # that care about that distinction pass period_start explicitly.
    if period_start is None and metric_code in _DURATION_METRICS:
        period_start = period_end - timedelta(days=90)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.fundamental_fact
                (entity_id, metric_code, period_start, period_end, fiscal_period,
                 value, unit, source, vendor_native_tag, filed_date, knowledge_from,
                 payload_id)
            VALUES (%s, %s, %s, %s, %s, %s, 'USD', 'edgar', %s, %s, %s, %s)
            """,
            (
                entity_id,
                metric_code,
                period_start,
                period_end,
                fiscal_period,
                value,
                vendor_native_tag,
                filed_date,
                datetime.combine(filed_date, datetime.min.time(), tzinfo=UTC) + timedelta(days=1),
                payload_id,
            ),
        )
    conn.commit()


# --- 1. Cross-vendor price ---------------------------------------------------


def test_cross_vendor_price_no_data_passes_vacuously(db_connection: psycopg.Connection) -> None:
    # Sources guaranteed to have zero rows in the shared session DB - other
    # tests (including pdw.backtest's) legitimately seed realistic
    # yfinance/tiingo price data, so asserting *global* emptiness for those
    # two real source names would be order-dependent.
    no_data_rules = [
        {**_RULES[0], "left": {"source": "no_such_vendor_a", "field": "close"},
         "right": {"source": "no_such_vendor_b", "field": "close"}}
    ]

    results = check_cross_vendor_price(db_connection, no_data_rules)

    assert len(results) == 1
    assert results[0].status == "pass"
    assert results[0].observed["compared_rows"] == 0


def _p(
    conn: psycopg.Connection, entity_id: int, d: date, close: float | None, source: str, pid: int
) -> None:
    _insert_price(
        conn, entity_id=entity_id, trade_date=d, close=close, source=source, payload_id=pid
    )


def _only(results: list[CheckResult], entity_id: int) -> list[CheckResult]:
    """Checks scan the whole live-shared test database, not just what this
    test seeded - filter down to this test's own entity before asserting."""
    return [r for r in results if r.entity_id == entity_id]


def test_cross_vendor_price_matching_values_pass(db_connection: psycopg.Connection) -> None:
    entity_id = _make_entity(db_connection, "5555555501")
    payload_id = _make_payload(db_connection)
    d = date(2024, 1, 2)
    _p(db_connection, entity_id, d, 100.0, "yfinance", payload_id)
    _p(db_connection, entity_id, d, 100.05, "tiingo", payload_id)

    results = _only(check_cross_vendor_price(db_connection, _RULES), entity_id)

    assert len(results) == 1
    assert results[0].status == "pass"


def test_cross_vendor_price_shifted_decimal_fails(db_connection: psycopg.Connection) -> None:
    """Seeded corruption: a 10x decimal shift between vendors."""
    entity_id = _make_entity(db_connection, "5555555502")
    payload_id = _make_payload(db_connection)
    d = date(2024, 1, 2)
    _p(db_connection, entity_id, d, 100.0, "yfinance", payload_id)
    _p(db_connection, entity_id, d, 1000.0, "tiingo", payload_id)

    results = _only(check_cross_vendor_price(db_connection, _RULES), entity_id)

    assert len(results) == 1
    assert results[0].status == "fail"
    assert results[0].severity == "WARN"  # below the 3-consecutive-day escalation threshold


def test_cross_vendor_price_nulled_price_fails(db_connection: psycopg.Connection) -> None:
    """Seeded corruption: a null close on one vendor's side."""
    entity_id = _make_entity(db_connection, "5555555503")
    payload_id = _make_payload(db_connection)
    d = date(2024, 1, 2)
    _p(db_connection, entity_id, d, 100.0, "yfinance", payload_id)
    _p(db_connection, entity_id, d, None, "tiingo", payload_id)

    results = _only(check_cross_vendor_price(db_connection, _RULES), entity_id)

    assert len(results) == 1
    assert results[0].status == "fail"


def test_cross_vendor_price_escalates_after_consecutive_days(
    db_connection: psycopg.Connection,
) -> None:
    entity_id = _make_entity(db_connection, "5555555504")
    payload_id = _make_payload(db_connection)

    for i in range(4):
        d = date(2024, 1, 2) + timedelta(days=i)
        _p(db_connection, entity_id, d, 100.0, "yfinance", payload_id)
        _p(db_connection, entity_id, d, 200.0, "tiingo", payload_id)

    results = _only(check_cross_vendor_price(db_connection, _RULES), entity_id)
    by_date = {r.dimension_key: r for r in results}
    last_key = sorted(by_date)[-1]

    assert all(r.status == "fail" for r in results)
    assert by_date[last_key].severity == "BREAK"  # 4th consecutive failure >= threshold of 3


def test_cross_vendor_price_uses_the_configured_field_not_always_close(
    db_connection: psycopg.Connection,
) -> None:
    """Regression: the check used to hardcode l.close/r.close in its SQL,
    silently ignoring the rule's own `field` key. Found live - yfinance's
    Close is always split-adjusted by Yahoo's backend while Tiingo's close
    is the true raw quote, so close diverges by ~10x for any split-affected
    ticker forever. A rule pointed at adj_close (the field both vendors
    agree on) must actually be honored, not silently compared on close.
    """
    entity_id = _make_entity(db_connection, "5555555517")
    payload_id = _make_payload(db_connection)
    d = date(2024, 1, 2)
    # close disagrees wildly (as if split-adjusted differently); adj_close
    # agrees closely - the rule below points at adj_close.
    _insert_price(
        db_connection, entity_id=entity_id, trade_date=d, close=1700.0, adj_close=170.0,
        source="yfinance", payload_id=payload_id,
    )
    _insert_price(
        db_connection, entity_id=entity_id, trade_date=d, close=170.0, adj_close=170.05,
        source="tiingo", payload_id=payload_id,
    )
    adj_close_rule = [
        {
            **_RULES[0],
            "left": {"source": "yfinance", "field": "adj_close"},
            "right": {"source": "tiingo", "field": "adj_close"},
        }
    ]

    results = _only(check_cross_vendor_price(db_connection, adj_close_rule), entity_id)

    assert len(results) == 1
    assert results[0].status == "pass"
    assert results[0].observed["left"] == 170.0


def test_cross_vendor_price_rejects_unsupported_field(db_connection: psycopg.Connection) -> None:
    bad_rule = [{**_RULES[0], "left": {"source": "yfinance", "field": "volume"}}]

    try:
        check_cross_vendor_price(db_connection, bad_rule)
        raise AssertionError("expected ValueError for an unsupported field")
    except ValueError as exc:
        assert "volume" in str(exc)


# --- 2. Balance sheet identity ------------------------------------------------


def _companyfacts_body(cik: int, period_end: str, assets: float, liab_and_equity: float) -> bytes:
    def point(val: float) -> dict[str, object]:
        return {
            "end": period_end,
            "val": val,
            "accn": "a",
            "fy": 2023,
            "fp": "FY",
            "form": "10-K",
            "filed": period_end,
        }

    return json.dumps(
        {
            "cik": cik,
            "entityName": "Test Corp",
            "facts": {
                "us-gaap": {
                    "Assets": {"units": {"USD": [point(assets)]}},
                    "LiabilitiesAndStockholdersEquity": {
                        "units": {"USD": [point(liab_and_equity)]}
                    },
                }
            },
        }
    ).encode("utf-8")


def test_balance_sheet_identity_no_data_passes_vacuously(db_connection: psycopg.Connection) -> None:
    results = check_balance_sheet_identity(db_connection)

    assert len(results) == 1
    assert results[0].status == "pass"


def test_balance_sheet_identity_matching_values_pass(db_connection: psycopg.Connection) -> None:
    cik = "5555555505"
    entity_id = _make_entity(db_connection, cik)
    period_end = date(2023, 12, 31)
    body = _companyfacts_body(int(cik), period_end.isoformat(), 1_000_000, 1_000_000)
    payload_id = _make_payload(db_connection, request_params={"cik": cik}, body=body)
    _insert_fundamental(
        db_connection,
        entity_id=entity_id,
        metric_code="total_assets",
        period_end=period_end,
        value=1_000_000,
        payload_id=payload_id,
    )

    results = _only(check_balance_sheet_identity(db_connection), entity_id)

    assert len(results) == 1
    assert results[0].status == "pass"


def test_balance_sheet_identity_shifted_decimal_fails(db_connection: psycopg.Connection) -> None:
    """Seeded corruption: total_assets off by a factor of 10 from the
    balance sheet's own liabilities+equity total."""
    cik = "5555555506"
    entity_id = _make_entity(db_connection, cik)
    period_end = date(2023, 12, 31)
    body = _companyfacts_body(int(cik), period_end.isoformat(), 1_000_000, 1_000_000)
    payload_id = _make_payload(db_connection, request_params={"cik": cik}, body=body)
    _insert_fundamental(
        db_connection,
        entity_id=entity_id,
        metric_code="total_assets",
        period_end=period_end,
        value=10_000_000,  # 10x shifted
        payload_id=payload_id,
    )

    results = _only(check_balance_sheet_identity(db_connection), entity_id)

    assert len(results) == 1
    assert results[0].status == "fail"
    assert results[0].severity == "BREAK"


# --- 3. Revenue sanity ---------------------------------------------------------


def test_revenue_sanity_normal_growth_passes(db_connection: psycopg.Connection) -> None:
    entity_id = _make_entity(db_connection, "5555555507")
    payload_id = _make_payload(db_connection)
    for i in range(6):
        _insert_fundamental(
            db_connection,
            entity_id=entity_id,
            metric_code="revenue",
            period_end=date(2022, 1, 1) + timedelta(days=90 * i),
            value=100 + i * 5,
            payload_id=payload_id,
        )

    results = check_revenue_sanity(db_connection)

    assert results
    assert all(r.status == "pass" for r in results)


def test_revenue_sanity_negative_value_fails(db_connection: psycopg.Connection) -> None:
    entity_id = _make_entity(db_connection, "5555555508")
    payload_id = _make_payload(db_connection)
    for i in range(4):
        _insert_fundamental(
            db_connection,
            entity_id=entity_id,
            metric_code="revenue",
            period_end=date(2022, 1, 1) + timedelta(days=90 * i),
            value=100,
            payload_id=payload_id,
        )
    _insert_fundamental(
        db_connection,
        entity_id=entity_id,
        metric_code="revenue",
        period_end=date(2023, 1, 1),
        value=-50,
        payload_id=payload_id,
    )

    results = check_revenue_sanity(db_connection)

    assert any(r.status == "fail" and r.severity == "WARN" for r in results)


def test_revenue_sanity_ytd_cumulative_figure_is_excluded_not_flagged(
    db_connection: psycopg.Connection,
) -> None:
    """Regression: found live against AAPL - a 10-Q also discloses the
    YTD-cumulative figure (e.g. 9 months, not just the 3-month quarter)
    under the same metric_code. Comparing that ~3x-larger cumulative value
    against a trailing median built from single quarters made every filer's
    YTD figure look like a magnitude outlier. It must be excluded from the
    comparison entirely (not merely pass), since it isn't a genuinely
    different quarter's revenue at all - and it must not pollute the
    trailing-median series used to judge the real quarters either.
    """
    entity_id = _make_entity(db_connection, "5555555515")
    payload_id = _make_payload(db_connection)
    # 5 single-quarter facts - the check only starts comparing once it has
    # 4 trailing points, so this is the minimum that exercises a real ratio.
    quarter_ends = [
        date(2022, 3, 31),
        date(2022, 6, 30),
        date(2022, 9, 30),
        date(2022, 12, 31),
        date(2023, 3, 31),
    ]
    for period_end in quarter_ends:
        _insert_fundamental(
            db_connection,
            entity_id=entity_id,
            metric_code="revenue",
            period_end=period_end,
            value=100,
            payload_id=payload_id,
        )
    # 9-month YTD figure as of the same Q3 period_end, ~3x a single
    # quarter's value - the shape that used to trip the outlier check.
    _insert_fundamental(
        db_connection,
        entity_id=entity_id,
        metric_code="revenue",
        period_end=date(2022, 9, 30),
        period_start=date(2022, 1, 1),
        value=300,
        payload_id=payload_id,
    )

    results = _only(check_revenue_sanity(db_connection), entity_id)

    assert len(results) == 1  # only the 5th quarter has 4 trailing points
    assert results[0].status == "pass"
    assert results[0].observed["trailing_median"] == 100  # undisturbed by the excluded YTD row


def test_revenue_sanity_sustained_growth_does_not_trip_the_unbounded_median(
    db_connection: psycopg.Connection,
) -> None:
    """Regression: found live against Microsoft - the trailing median used
    to look back across a company's *entire* history (values[:i]), not a
    bounded window. A company that simply grew for a decade (MSFT's
    quarterly revenue is roughly 3.5x higher in 2024-2025 than in its
    slow-growth era) trips a ratio > 3.0 against that whole-history median
    forever, once enough growth has compounded - that's sustained growth,
    not an anomaly. An 8-quarter rolling window must judge the latest
    quarter against its own recent regime, not the company's oldest years.
    """
    entity_id = _make_entity(db_connection, "5555555518")
    payload_id = _make_payload(db_connection)
    # 10 quarters of a flat, slow-growth era, then 6 quarters of a much
    # larger, but internally stable, growth-era regime.
    values = [100.0] * 10 + [350.0] * 6
    for i, value in enumerate(values):
        _insert_fundamental(
            db_connection,
            entity_id=entity_id,
            metric_code="revenue",
            period_end=date(2019, 3, 31) + timedelta(days=91 * i),
            value=value,
            payload_id=payload_id,
        )

    results = _only(check_revenue_sanity(db_connection), entity_id)
    latest = max(results, key=lambda r: r.dimension_key)

    assert latest.status == "pass"
    assert latest.observed["trailing_median"] == 350.0  # last 8 quarters, not the oldest 10


# --- 4. Period coverage gaps ---------------------------------------------------


def test_period_coverage_no_gaps_passes(db_connection: psycopg.Connection) -> None:
    entity_id = _make_entity(db_connection, "5555555509")
    payload_id = _make_payload(db_connection)
    for i in range(4):
        _insert_fundamental(
            db_connection,
            entity_id=entity_id,
            metric_code="revenue",
            period_end=date(2023, 3, 31) + timedelta(days=91 * i),
            fiscal_period="Q1",
            value=100,
            payload_id=payload_id,
        )

    results = check_period_coverage_gaps(db_connection)

    assert results
    assert all(r.status == "pass" for r in results)


def test_period_coverage_dropped_quarter_fails(db_connection: psycopg.Connection) -> None:
    """Seeded corruption: a quarter is entirely missing from the sequence."""
    entity_id = _make_entity(db_connection, "5555555510")
    payload_id = _make_payload(db_connection)
    quarters = [date(2023, 3, 31), date(2023, 6, 30), date(2023, 12, 31)]  # Q3 dropped
    for period_end in quarters:
        _insert_fundamental(
            db_connection,
            entity_id=entity_id,
            metric_code="revenue",
            period_end=period_end,
            fiscal_period="Q1",
            value=100,
            payload_id=payload_id,
        )

    results = check_period_coverage_gaps(db_connection)

    assert any(r.status == "fail" and r.severity == "WARN" for r in results)


def test_period_coverage_annual_figure_bridges_the_undisclosed_q4(
    db_connection: psycopg.Connection,
) -> None:
    """Regression: found live against the full universe - almost no filer
    discloses a standalone Q4 duration fact (Q4 = FY - Q1 - Q2 - Q3 by
    convention, never filed on its own). Q1/Q2/Q3 alone made the Q3->next-Q1
    transition look like a dropped quarter for every filer, every year. The
    annual 'FY' fact (same period_end as where Q4 would have landed) must
    close that gap.
    """
    entity_id = _make_entity(db_connection, "5555555516")
    payload_id = _make_payload(db_connection)
    _insert_fundamental(
        db_connection, entity_id=entity_id, metric_code="revenue",
        period_end=date(2022, 3, 31), fiscal_period="Q1", value=100, payload_id=payload_id,
    )
    _insert_fundamental(
        db_connection, entity_id=entity_id, metric_code="revenue",
        period_end=date(2022, 6, 30), fiscal_period="Q2", value=100, payload_id=payload_id,
    )
    _insert_fundamental(
        db_connection, entity_id=entity_id, metric_code="revenue",
        period_end=date(2022, 9, 30), fiscal_period="Q3", value=100, payload_id=payload_id,
    )
    _insert_fundamental(
        db_connection, entity_id=entity_id, metric_code="revenue",
        period_end=date(2022, 12, 31), fiscal_period="FY", value=400,
        period_start=date(2022, 1, 1), payload_id=payload_id,
    )
    _insert_fundamental(
        db_connection, entity_id=entity_id, metric_code="revenue",
        period_end=date(2023, 3, 31), fiscal_period="Q1", value=100, payload_id=payload_id,
    )

    results = _only(check_period_coverage_gaps(db_connection), entity_id)

    assert results
    assert all(r.status == "pass" for r in results)


# --- 5. Price staleness --------------------------------------------------------


def test_price_staleness_fresh_price_passes(db_connection: psycopg.Connection) -> None:
    entity_id = _make_entity(db_connection, "5555555511")
    payload_id = _make_payload(db_connection)
    as_of = datetime(2024, 6, 10, tzinfo=UTC)
    _p(db_connection, entity_id, date(2024, 6, 7), 100, "yfinance", payload_id)

    results = _only(check_price_staleness(db_connection, as_of=as_of), entity_id)

    assert len(results) == 1
    assert results[0].status == "pass"


def test_price_staleness_stale_feed_fails(db_connection: psycopg.Connection) -> None:
    """Seeded corruption: the price feed hasn't updated in weeks."""
    entity_id = _make_entity(db_connection, "5555555512")
    payload_id = _make_payload(db_connection)
    as_of = datetime(2024, 7, 1, tzinfo=UTC)
    _p(db_connection, entity_id, date(2024, 6, 1), 100, "yfinance", payload_id)

    results = _only(check_price_staleness(db_connection, as_of=as_of), entity_id)

    assert len(results) == 1
    assert results[0].status == "fail"
    assert results[0].severity == "BREAK"


# --- 6. Return outliers ---------------------------------------------------------


def test_return_outliers_extreme_move_fails(db_connection: psycopg.Connection) -> None:
    entity_id = _make_entity(db_connection, "5555555513")
    payload_id = _make_payload(db_connection)
    base_date = date(2024, 1, 2)
    # Small realistic day-to-day variance, not constant - a zero-variance
    # trailing window is deliberately excluded by the check (it would
    # otherwise divide by a zero stddev), so an all-identical series can
    # never produce a computable z-score.
    for i in range(30):
        close = 100.0 + (1.0 if i % 2 == 0 else -1.0)
        _p(db_connection, entity_id, base_date + timedelta(days=i), close, "yfinance", payload_id)
    _p(db_connection, entity_id, base_date + timedelta(days=30), 1000.0, "yfinance", payload_id)

    results = _only(check_return_outliers(db_connection), entity_id)

    assert any(r.status == "fail" and r.severity == "INFO" for r in results)


# --- 7. Payload freshness -------------------------------------------------------


def test_payload_freshness_unchanged_when_change_expected_fails(
    db_connection: psycopg.Connection,
) -> None:
    """Seeded corruption: the same body fetched twice, far enough apart
    that a real change should have shown up (e.g. a stale/broken feed)."""
    with db_connection.cursor() as cur:
        cur.execute("INSERT INTO ops.pipeline_run (pipeline) VALUES ('test') RETURNING run_id")
        row = cur.fetchone()
        assert row is not None
        run_id = row[0]
        for fetched_at in [datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 5, 1, tzinfo=UTC)]:
            cur.execute(
                """
                INSERT INTO raw.payload
                    (source, endpoint, request_params, fetched_at, http_status,
                     content_sha256, body, run_id)
                VALUES ('yfinance', 'history', %s, %s, 200, repeat('0', 64), 'x', %s)
                """,
                (Jsonb({"ticker": "STALEFEED"}), fetched_at, run_id),
            )
    db_connection.commit()

    results = check_payload_freshness(db_connection)

    assert any(r.status == "fail" and r.severity == "WARN" for r in results)


# --- 8. Tag switches -------------------------------------------------------------


def test_tag_switch_detected(db_connection: psycopg.Connection) -> None:
    entity_id = _make_entity(db_connection, "5555555514")
    payload_id = _make_payload(db_connection)
    _insert_fundamental(
        db_connection, entity_id=entity_id, metric_code="revenue", period_end=date(2017, 12, 31),
        value=100, payload_id=payload_id, vendor_native_tag="Revenues",
    )
    _insert_fundamental(
        db_connection, entity_id=entity_id, metric_code="revenue", period_end=date(2018, 12, 31),
        value=110, payload_id=payload_id,
        vendor_native_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
    )

    results = check_tag_switches(db_connection)

    assert any(r.status == "fail" and r.severity == "INFO" for r in results)
