from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import yaml
from psycopg import sql


@dataclass(frozen=True)
class CheckResult:
    """One row of dq.check_result. CLAUDE.md 7: every check writes a result
    every run, including passes - `dimension_key` is what a failing result
    groups under in dq.exception across runs (e.g. a ticker, or
    ticker+period); non-entity-scoped summaries use "__global__".
    """

    check_name: str
    dataset: str
    severity: str
    status: str  # "pass" | "fail"
    observed: dict[str, Any]
    expected: dict[str, Any]
    entity_id: int | None = None
    dimension_key: str = "__global__"


# ---------------------------------------------------------------------------
# 1. Cross-vendor close price within tolerance (cross-source, WARN -> BREAK)
# ---------------------------------------------------------------------------


def load_reconciliation_rules(path: Any) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"reconciliation config {path} must be a list of rules")
    return data


# Columns a reconciliation rule is allowed to name in config/reconciliation.yaml's
# `field` key - an allowlist because the column name is interpolated as a SQL
# identifier (psycopg can't parameterize identifiers, only values).
_RECONCILABLE_PRICE_FIELDS = {"open", "high", "low", "close", "adj_close"}


def check_cross_vendor_price(
    conn: psycopg.Connection, rules: list[dict[str, Any]]
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for rule in rules:
        name = rule["name"]
        left_source, right_source = rule["left"]["source"], rule["right"]["source"]
        left_field, right_field = rule["left"]["field"], rule["right"]["field"]
        if left_field not in _RECONCILABLE_PRICE_FIELDS:
            raise ValueError(f"rule {name!r}: unsupported left field {left_field!r}")
        if right_field not in _RECONCILABLE_PRICE_FIELDS:
            raise ValueError(f"rule {name!r}: unsupported right field {right_field!r}")
        tolerance = rule["tolerance"]["value"]
        base_severity = rule["severity"]

        # yfinance's Close is always split-adjusted by Yahoo's own backend
        # regardless of the auto_adjust fetch flag (confirmed live: AVGO's
        # pre-2024-07-15-split dates already read in post-split scale),
        # while Tiingo's close is the true raw historical quote - the two
        # vendors' `close` fields aren't the same quantity for any ticker
        # that's split within the fetch window, and never converge. Query
        # the rule's own configured field (typically adj_close, the one
        # quantity both vendors agree on) rather than assuming `close`.
        query = sql.SQL(
            """
            SELECT l.entity_id, e.cik, l.trade_date, l.{left_field}, r.{right_field}
            FROM core.price_fact l
            JOIN core.price_fact r
                ON r.entity_id = l.entity_id AND r.trade_date = l.trade_date
               AND r.source = %s AND r.knowledge_to = 'infinity'
            JOIN core.entity e ON e.entity_id = l.entity_id
            WHERE l.source = %s AND l.knowledge_to = 'infinity'
            ORDER BY l.entity_id, l.trade_date
            """
        ).format(
            left_field=sql.Identifier(left_field), right_field=sql.Identifier(right_field)
        )

        with conn.cursor() as cur:
            cur.execute(query, (right_source, left_source))
            rows = cur.fetchall()

        if not rows:
            results.append(
                CheckResult(
                    check_name=name,
                    dataset="core.price_fact",
                    severity=base_severity,
                    status="pass",
                    observed={"compared_rows": 0},
                    expected={
                        "note": f"no comparable {left_source}/{right_source} rows yet"
                    },
                )
            )
            continue

        rule_results = []
        for entity_id, cik, trade_date, left_val, right_val in rows:
            dimension_key = f"{cik}:{trade_date.isoformat()}"
            if left_val is None or right_val is None:
                # A missing price on either side is itself a data-quality
                # failure, not something to silently skip - div-by-None
                # would crash below anyway.
                rule_results.append(
                    CheckResult(
                        check_name=name,
                        dataset="core.price_fact",
                        severity=base_severity,
                        status="fail",
                        observed={
                            "left": float(left_val) if left_val is not None else None,
                            "right": float(right_val) if right_val is not None else None,
                        },
                        expected={"note": "both sides must be non-null to compare"},
                        entity_id=entity_id,
                        dimension_key=dimension_key,
                    )
                )
                continue

            right_f = float(right_val)
            rel_diff = abs(float(left_val) - right_f) / abs(right_f) if right_f else None
            passed = rel_diff is not None and rel_diff <= tolerance
            rule_results.append(
                CheckResult(
                    check_name=name,
                    dataset="core.price_fact",
                    severity=base_severity,
                    status="pass" if passed else "fail",
                    observed={
                        "left": float(left_val),
                        "right": right_f,
                        "relative_diff": rel_diff,
                    },
                    expected={"tolerance_type": "relative", "tolerance": tolerance},
                    entity_id=entity_id,
                    dimension_key=dimension_key,
                )
            )
        results.extend(_escalate_consecutive_failures(rule_results, rule.get("escalate_if")))
    return results


def _escalate_consecutive_failures(
    results: list[CheckResult], escalate_if: dict[str, Any] | None
) -> list[CheckResult]:
    if not escalate_if:
        return results

    threshold = escalate_if["consecutive_days"]
    escalated_severity = escalate_if["severity"]

    by_entity: dict[int | None, list[CheckResult]] = defaultdict(list)
    for r in results:
        by_entity[r.entity_id].append(r)

    out: list[CheckResult] = []
    for entity_results in by_entity.values():
        entity_results.sort(key=lambda r: r.dimension_key)
        streak = 0
        for r in entity_results:
            streak = streak + 1 if r.status == "fail" else 0
            out.append(replace(r, severity=escalated_severity) if streak >= threshold else r)
    return out


# ---------------------------------------------------------------------------
# 2. Balance sheet identity: assets ~= liabilities + equity (intra-record, BREAK)
# ---------------------------------------------------------------------------

# LiabilitiesAndStockholdersEquity is a standard us-gaap tag reporting
# Liabilities + Equity as a single combined figure (the balance sheet's own
# "Total liabilities and stockholders' equity" line). Using it means this
# check needs no data beyond what's already in raw.payload - it deliberately
# is *not* added to config/metric_map.yaml as a 7th tracked metric (CLAUDE.md
# 2's "Fundamental metrics: 6" is a hard scope limit on what's queryable via
# PointInTimeReader/the backtest); it's parsed here, once, purely as this
# check's internal cross-validation input.
_LIABILITIES_AND_EQUITY_TAG = "LiabilitiesAndStockholdersEquity"
_BALANCE_SHEET_TOLERANCE = 0.01  # relative; accounting identities should match almost exactly


def check_balance_sheet_identity(conn: psycopg.Connection) -> list[CheckResult]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.entity_id, e.cik, f.period_end, f.value
            FROM core.fundamental_fact f
            JOIN core.entity e ON e.entity_id = f.entity_id
            WHERE f.metric_code = 'total_assets' AND f.source = 'edgar'
                  AND f.knowledge_to = 'infinity'
            """
        )
        assets_rows = cur.fetchall()

    if not assets_rows:
        return [
            CheckResult(
                check_name="balance_sheet_identity",
                dataset="core.fundamental_fact",
                severity="BREAK",
                status="pass",
                observed={"compared_rows": 0},
                expected={"note": "no total_assets facts loaded yet"},
            )
        ]

    results: list[CheckResult] = []
    body_cache: dict[str, bytes] = {}
    for entity_id, cik, period_end, assets_value in assets_rows:
        body = body_cache.get(cik)
        if body is None:
            body = _latest_companyfacts_body(conn, cik)
            if body is not None:
                body_cache[cik] = body
        if body is None:
            continue

        liab_and_equity = _extract_instant_value(body, _LIABILITIES_AND_EQUITY_TAG, period_end)
        if liab_and_equity is None:
            continue

        assets_f = float(assets_value)
        rel_diff = abs(assets_f - liab_and_equity) / abs(assets_f) if assets_f else None
        passed = rel_diff is not None and rel_diff <= _BALANCE_SHEET_TOLERANCE
        results.append(
            CheckResult(
                check_name="balance_sheet_identity",
                dataset="core.fundamental_fact",
                severity="BREAK",
                status="pass" if passed else "fail",
                observed={"assets": assets_f, "liabilities_and_equity": liab_and_equity},
                expected={"tolerance_type": "relative", "tolerance": _BALANCE_SHEET_TOLERANCE},
                entity_id=entity_id,
                dimension_key=f"{cik}:{period_end.isoformat()}",
            )
        )

    if not results:
        results.append(
            CheckResult(
                check_name="balance_sheet_identity",
                dataset="core.fundamental_fact",
                severity="BREAK",
                status="pass",
                observed={"compared_rows": 0},
                expected={"note": f"no {_LIABILITIES_AND_EQUITY_TAG} facts found"},
            )
        )
    return results


def _latest_companyfacts_body(conn: psycopg.Connection, cik: str) -> bytes | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.body FROM raw.payload p
            WHERE p.source = 'edgar' AND p.endpoint = 'companyfacts'
                  AND p.request_params->>'cik' = %s AND p.http_status = 200
            ORDER BY p.fetched_at DESC LIMIT 1
            """,
            (cik,),
        )
        row = cur.fetchone()
        return bytes(row[0]) if row else None


def _extract_instant_value(body: bytes, tag: str, period_end: date) -> float | None:
    data = json.loads(body)
    tag_body = data.get("facts", {}).get("us-gaap", {}).get(tag)
    if tag_body is None:
        return None
    for point in tag_body.get("units", {}).get("USD", []):
        if point.get("end") == period_end.isoformat() and point.get("start") is None:
            return float(point["val"])
    return None


# ---------------------------------------------------------------------------
# 3. Revenue sign and magnitude sanity vs trailing median (statistical, WARN)
# ---------------------------------------------------------------------------


# A 10-Q also discloses YTD-cumulative figures (e.g. a "Q3" fact spanning
# Jan1-Sep30, not just Jul1-Sep30) and a 10-K discloses the full-year figure
# under the same metric_code - both share fiscal_period labels with genuine
# single-quarter facts, so fiscal_period alone can't distinguish them
# (confirmed live: AAPL's 9-month YTD revenue was flagged as a 4-5x "outlier"
# against a trailing median built from single quarters). Duration is the
# only reliable discriminator.
_SINGLE_QUARTER_MIN_DAYS = 80
_SINGLE_QUARTER_MAX_DAYS = 100

# Rolling, not whole-history: found live against Microsoft - a company that
# has simply grown for a decade (quarterly revenue roughly 3.5x higher in
# 2024-2025 than in the early 2010s) trips an *unbounded* trailing median
# forever, since the median of an ever-growing prefix falls further behind
# the current value the longer the growth compounds. That flags sustained
# organic growth, not an anomaly. 8 quarters (2 years) is enough history for
# a stable median while staying responsive to genuine trend.
_TRAILING_WINDOW_QUARTERS = 8


def check_revenue_sanity(conn: psycopg.Connection) -> list[CheckResult]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.entity_id, e.cik, f.period_end, f.value
            FROM core.fundamental_fact f
            JOIN core.entity e ON e.entity_id = f.entity_id
            WHERE f.metric_code = 'revenue' AND f.source = 'edgar'
                  AND f.knowledge_to = 'infinity'
                  AND f.period_start IS NOT NULL
                  AND (f.period_end - f.period_start)
                      BETWEEN %s AND %s
            ORDER BY f.entity_id, f.period_end
            """,
            (_SINGLE_QUARTER_MIN_DAYS, _SINGLE_QUARTER_MAX_DAYS),
        )
        rows = cur.fetchall()

    if not rows:
        return [
            CheckResult(
                check_name="revenue_sanity",
                dataset="core.fundamental_fact",
                severity="WARN",
                status="pass",
                observed={"compared_rows": 0},
                expected={"note": "no revenue facts loaded yet"},
            )
        ]

    by_entity: dict[int, list[tuple[str, date, float]]] = defaultdict(list)
    for entity_id, cik, period_end, value in rows:
        by_entity[entity_id].append((cik, period_end, float(value)))

    results: list[CheckResult] = []
    for entity_id, series in by_entity.items():
        values = [v for _, _, v in series]
        for i, (cik, period_end, value) in enumerate(series):
            trailing = values[max(0, i - _TRAILING_WINDOW_QUARTERS) : i]
            if len(trailing) < 4:
                continue
            median = sorted(trailing)[len(trailing) // 2]
            ratio = value / median if median else None
            is_negative = value < 0
            is_outlier = ratio is not None and not (0.3 <= ratio <= 3.0)
            failed = is_negative or is_outlier
            results.append(
                CheckResult(
                    check_name="revenue_sanity",
                    dataset="core.fundamental_fact",
                    severity="WARN",
                    status="fail" if failed else "pass",
                    observed={"value": value, "trailing_median": median, "ratio": ratio},
                    expected={"sign": "non-negative", "ratio_range": [0.3, 3.0]},
                    entity_id=entity_id,
                    dimension_key=f"{cik}:{period_end.isoformat()}",
                )
            )

    if not results:
        results.append(
            CheckResult(
                check_name="revenue_sanity",
                dataset="core.fundamental_fact",
                severity="WARN",
                status="pass",
                observed={"compared_rows": 0},
                expected={"note": "not enough trailing history yet"},
            )
        )
    return results


# ---------------------------------------------------------------------------
# 4. Fundamental period coverage gaps / missing quarter (completeness, WARN)
# ---------------------------------------------------------------------------

_QUARTER_GAP_THRESHOLD_DAYS = 130  # ~1 quarter (91d) plus slack for filing-date jitter


def check_period_coverage_gaps(conn: psycopg.Connection) -> list[CheckResult]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT f.entity_id, e.cik, f.metric_code, f.period_end
            FROM core.fundamental_fact f
            JOIN core.entity e ON e.entity_id = f.entity_id
            WHERE f.source = 'edgar' AND f.knowledge_to = 'infinity'
                  -- 'FY' included deliberately: almost no filer discloses a
                  -- standalone Q4 duration fact (it's derived as FY - Q1 -
                  -- Q2 - Q3 by convention, never filed on its own), so
                  -- excluding FY made every filer's Q3->next-Q1 transition
                  -- look like a dropped quarter every single year - a
                  -- systematic false positive, confirmed live against the
                  -- full 50-ticker universe.
                  AND f.fiscal_period IN ('Q1', 'Q2', 'Q3', 'Q4', 'FY')
            ORDER BY f.entity_id, f.metric_code, f.period_end
            """
        )
        rows = cur.fetchall()

    if not rows:
        return [
            CheckResult(
                check_name="period_coverage_gaps",
                dataset="core.fundamental_fact",
                severity="WARN",
                status="pass",
                observed={"compared_rows": 0},
                expected={"note": "no quarterly facts loaded yet"},
            )
        ]

    by_key: dict[tuple[int, str], list[tuple[str, date]]] = defaultdict(list)
    for entity_id, cik, metric_code, period_end in rows:
        by_key[(entity_id, metric_code)].append((cik, period_end))

    results: list[CheckResult] = []
    for (entity_id, metric_code), series in by_key.items():
        cik = series[0][0]
        for (_, prev_end), (_, cur_end) in zip(series, series[1:], strict=False):
            gap_days = (cur_end - prev_end).days
            failed = gap_days > _QUARTER_GAP_THRESHOLD_DAYS
            results.append(
                CheckResult(
                    check_name="period_coverage_gaps",
                    dataset="core.fundamental_fact",
                    severity="WARN",
                    status="fail" if failed else "pass",
                    observed={"gap_days": gap_days, "metric_code": metric_code},
                    expected={"max_gap_days": _QUARTER_GAP_THRESHOLD_DAYS},
                    entity_id=entity_id,
                    dimension_key=f"{cik}:{metric_code}:{prev_end.isoformat()}-{cur_end.isoformat()}",
                )
            )

    if not results:
        results.append(
            CheckResult(
                check_name="period_coverage_gaps",
                dataset="core.fundamental_fact",
                severity="WARN",
                status="pass",
                observed={"compared_rows": 0},
                expected={"note": "not enough periods yet to compare gaps"},
            )
        )
    return results


# ---------------------------------------------------------------------------
# 5. Price staleness vs NYSE calendar (timeliness, BREAK)
# ---------------------------------------------------------------------------

_STALENESS_THRESHOLD_BUSINESS_DAYS = 5


def check_price_staleness(
    conn: psycopg.Connection, as_of: datetime | None = None
) -> list[CheckResult]:
    as_of = as_of or datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.entity_id, e.cik, max(p.trade_date)
            FROM core.price_fact p
            JOIN core.entity e ON e.entity_id = p.entity_id
            WHERE p.source = 'yfinance' AND p.knowledge_to = 'infinity'
            GROUP BY p.entity_id, e.cik
            """
        )
        rows = cur.fetchall()

    if not rows:
        return [
            CheckResult(
                check_name="price_staleness",
                dataset="core.price_fact",
                severity="BREAK",
                status="pass",
                observed={"compared_rows": 0},
                expected={"note": "no price facts loaded yet"},
            )
        ]

    results = []
    for entity_id, cik, max_trade_date in rows:
        business_days_stale = _count_business_days_between(max_trade_date, as_of.date())
        failed = business_days_stale > _STALENESS_THRESHOLD_BUSINESS_DAYS
        results.append(
            CheckResult(
                check_name="price_staleness",
                dataset="core.price_fact",
                severity="BREAK",
                status="fail" if failed else "pass",
                observed={
                    "max_trade_date": max_trade_date.isoformat(),
                    "business_days_stale": business_days_stale,
                },
                expected={"max_business_days_stale": _STALENESS_THRESHOLD_BUSINESS_DAYS},
                entity_id=entity_id,
                dimension_key=cik,
            )
        )
    return results


def _count_business_days_between(start: date, end: date) -> int:
    """Weekday-only count (CLAUDE.md's NYSE-calendar checks share the same
    documented simplification as pdw.availability: no holiday calendar)."""
    if end <= start:
        return 0
    count = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:
            count += 1
    return count


# ---------------------------------------------------------------------------
# 6. Volume/return outliers (z-score vs 250d window) (statistical, INFO)
# ---------------------------------------------------------------------------

_ZSCORE_THRESHOLD = 4.0
_ZSCORE_WINDOW = 250


def check_return_outliers(conn: psycopg.Connection) -> list[CheckResult]:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH returns AS (
                SELECT p.entity_id, e.cik, p.trade_date,
                       p.close / NULLIF(lag(p.close) OVER w, 0) - 1 AS daily_return
                FROM core.price_fact p
                JOIN core.entity e ON e.entity_id = p.entity_id
                WHERE p.source = 'yfinance' AND p.knowledge_to = 'infinity'
                WINDOW w AS (PARTITION BY p.entity_id ORDER BY p.trade_date)
            ),
            stats AS (
                SELECT *,
                       avg(daily_return) OVER w2 AS trailing_mean,
                       stddev_samp(daily_return) OVER w2 AS trailing_stddev
                FROM returns
                WINDOW w2 AS (
                    PARTITION BY entity_id ORDER BY trade_date
                    ROWS BETWEEN %s PRECEDING AND 1 PRECEDING
                )
            )
            SELECT entity_id, cik, trade_date, daily_return, trailing_mean, trailing_stddev
            FROM stats
            WHERE daily_return IS NOT NULL AND trailing_stddev IS NOT NULL
                  AND trailing_stddev > 0
            """,
            (_ZSCORE_WINDOW,),
        )
        rows = cur.fetchall()

    if not rows:
        return [
            CheckResult(
                check_name="return_outliers",
                dataset="core.price_fact",
                severity="INFO",
                status="pass",
                observed={"compared_rows": 0},
                expected={"note": "not enough price history yet for a trailing window"},
            )
        ]

    results = []
    for entity_id, cik, trade_date, daily_return, mean, stddev in rows:
        z = (float(daily_return) - float(mean)) / float(stddev)
        failed = abs(z) > _ZSCORE_THRESHOLD
        results.append(
            CheckResult(
                check_name="return_outliers",
                dataset="core.price_fact",
                severity="INFO",
                status="fail" if failed else "pass",
                observed={"daily_return": float(daily_return), "z_score": z},
                expected={"z_score_threshold": _ZSCORE_THRESHOLD},
                entity_id=entity_id,
                dimension_key=f"{cik}:{trade_date.isoformat()}",
            )
        )
    return results


# ---------------------------------------------------------------------------
# 7. Payload hash unchanged when change was expected (freshness, WARN)
# ---------------------------------------------------------------------------

_EXPECTED_REFRESH_DAYS = {"edgar": 100, "yfinance": 3, "tiingo": 3}


def check_payload_freshness(
    conn: psycopg.Connection, as_of: datetime | None = None
) -> list[CheckResult]:
    as_of = as_of or datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source, endpoint, request_params->>'ticker' AS ticker,
                   content_sha256, fetched_at,
                   lag(content_sha256) OVER w AS prev_hash,
                   lag(fetched_at) OVER w AS prev_fetched_at
            FROM raw.payload
            WHERE request_params ? 'ticker'
            WINDOW w AS (
                PARTITION BY source, endpoint, request_params->>'ticker'
                ORDER BY fetched_at
            )
            ORDER BY source, endpoint, ticker, fetched_at
            """
        )
        rows = cur.fetchall()

    if not rows:
        return [
            CheckResult(
                check_name="payload_freshness",
                dataset="raw.payload",
                severity="WARN",
                status="pass",
                observed={"compared_rows": 0},
                expected={"note": "no per-ticker payloads yet"},
            )
        ]

    results = []
    for source, endpoint, ticker, content_hash, fetched_at, prev_hash, prev_fetched_at in rows:
        if prev_hash is None:
            continue  # first-ever fetch for this key - nothing to compare yet
        expected_days = _EXPECTED_REFRESH_DAYS.get(source, 7)
        days_since_prev = (fetched_at - prev_fetched_at).days
        unchanged = content_hash == prev_hash
        change_was_expected = days_since_prev >= expected_days
        failed = unchanged and change_was_expected
        results.append(
            CheckResult(
                check_name="payload_freshness",
                dataset="raw.payload",
                severity="WARN",
                status="fail" if failed else "pass",
                observed={"unchanged": unchanged, "days_since_previous_fetch": days_since_prev},
                expected={"expected_refresh_days": expected_days},
                dimension_key=f"{source}:{endpoint}:{ticker}:{fetched_at.isoformat()}",
            )
        )

    if not results:
        results.append(
            CheckResult(
                check_name="payload_freshness",
                dataset="raw.payload",
                severity="WARN",
                status="pass",
                observed={"compared_rows": 0},
                expected={"note": "only one fetch per key so far"},
            )
        )
    return results


# ---------------------------------------------------------------------------
# 8. XBRL tag switched for an entity mid-history (metadata, INFO)
# ---------------------------------------------------------------------------


def check_tag_switches(conn: psycopg.Connection) -> list[CheckResult]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.entity_id, e.cik, f.metric_code,
                   array_agg(DISTINCT f.vendor_native_tag ORDER BY f.vendor_native_tag)
            FROM core.fundamental_fact f
            JOIN core.entity e ON e.entity_id = f.entity_id
            WHERE f.source = 'edgar' AND f.vendor_native_tag IS NOT NULL
            GROUP BY f.entity_id, e.cik, f.metric_code
            """
        )
        rows = cur.fetchall()

    if not rows:
        return [
            CheckResult(
                check_name="tag_switches",
                dataset="core.fundamental_fact",
                severity="INFO",
                status="pass",
                observed={"compared_rows": 0},
                expected={"note": "no fundamental facts loaded yet"},
            )
        ]

    results = []
    for entity_id, cik, metric_code, tags in rows:
        switched = len(tags) > 1
        results.append(
            CheckResult(
                check_name="tag_switches",
                dataset="core.fundamental_fact",
                severity="INFO",
                status="fail" if switched else "pass",
                observed={"tags_used": tags},
                expected={"tags_used": 1},
                entity_id=entity_id,
                dimension_key=f"{cik}:{metric_code}",
            )
        )
    return results
