from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg

from pdw.metric_map import MetricMapping
from pdw.sources import normalize_ticker_for_vendor
from pdw.sources.edgar import parse_ticker_map

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedFact:
    """One (metric, period) datapoint, after the metric map has picked its tag."""

    cik: str
    entity_name: str
    metric_code: str
    period_start: date | None
    period_end: date
    fiscal_year: int | None
    fiscal_period: str | None
    value: float
    unit: str
    vendor_native_tag: str
    form_type: str
    accession_no: str
    filed_date: date
    payload_id: int


@dataclass(frozen=True)
class ParseSummary:
    entities_parsed: int
    facts_written: int
    tickers_without_cik: tuple[str, ...]
    tickers_without_companyfacts: tuple[str, ...]


def parse_companyfacts(
    body: bytes, metric_map: dict[str, MetricMapping], payload_id: int
) -> list[ParsedFact]:
    """Apply the metric map to one entity's raw companyfacts JSON.

    Tag priority is resolved *per reporting period*, not once for the whole
    metric: verified live (2026-07-20) that filers genuinely switch tags
    mid-history (e.g. AAPL reported revenue under `Revenues` through fiscal
    2017, then switched to `RevenueFromContractWithCustomerExcludingAssessed
    Tax` from fiscal 2018 on, following ASC 606 adoption). Picking one tag
    for the entire company - the first version of this function did -
    silently drops every period the winning tag doesn't cover. Instead, for
    each distinct (period_start, period_end), the highest-priority tag that
    has *any* datapoint for that exact period wins; a lower-priority tag
    only fills in periods no higher-priority tag reports. vendor_native_tag
    is recorded per fact, so which tag won for which period stays visible
    (CLAUDE.md 4.1: "must be visible in the data, not smoothed over").
    """
    data = json.loads(body)
    cik = str(data["cik"]).zfill(10)
    entity_name = data["entityName"]
    gaap = data.get("facts", {}).get("us-gaap", {})

    facts: list[ParsedFact] = []
    for metric_code, mapping in metric_map.items():
        points_by_tag: dict[str, list[dict[str, Any]]] = {}
        owning_tag_by_period: dict[tuple[Any, Any], str] = {}

        for tag in mapping.tags:
            tag_body = gaap.get(tag)
            if tag_body is None:
                continue
            datapoints = tag_body.get("units", {}).get(mapping.unit)
            if not datapoints:
                continue

            points_by_tag[tag] = datapoints
            for point in datapoints:
                period_key = (point.get("start"), point["end"])
                owning_tag_by_period.setdefault(period_key, tag)

        for tag, datapoints in points_by_tag.items():
            for point in datapoints:
                period_key = (point.get("start"), point["end"])
                if owning_tag_by_period[period_key] != tag:
                    continue  # a higher-priority tag already covers this period

                start_raw = point.get("start")
                facts.append(
                    ParsedFact(
                        cik=cik,
                        entity_name=entity_name,
                        metric_code=metric_code,
                        period_start=date.fromisoformat(start_raw) if start_raw else None,
                        period_end=date.fromisoformat(point["end"]),
                        # fy/fp are genuinely absent (null, not just missing)
                        # on some real datapoints - verified live: every case
                        # observed across the 50-ticker universe is a `form:
                        # "8-K"` recasting filing, which sits outside the
                        # regular quarterly/annual cadence EDGAR assigns fy/fp
                        # to. core.fundamental_fact's schema already declares
                        # both columns nullable for exactly this reason.
                        fiscal_year=point.get("fy"),
                        fiscal_period=point.get("fp"),
                        value=point["val"],
                        unit=mapping.unit,
                        vendor_native_tag=tag,
                        form_type=point["form"],
                        accession_no=point["accn"],
                        filed_date=date.fromisoformat(point["filed"]),
                        payload_id=payload_id,
                    )
                )
    return facts


def _extract_entity_name(body: bytes) -> str:
    data = json.loads(body)
    name: str = data["entityName"]
    return name


def latest_payload(
    conn: psycopg.Connection, *, source: str, endpoint: str, ticker: str | None = None
) -> tuple[int, bytes, datetime] | None:
    """The most recent raw.payload row for (source, endpoint[, ticker]).

    raw.payload keeps every fetch ever made (append-only); parsing always
    works from the latest one, since EDGAR's companyfacts response already
    contains full point-in-time history per datapoint (filed/accn), so a
    single fetch is enough to reconstruct restatement history in M4.
    """
    with conn.cursor() as cur:
        if ticker is None:
            cur.execute(
                """
                SELECT payload_id, body, fetched_at FROM raw.payload
                WHERE source = %s AND endpoint = %s
                ORDER BY fetched_at DESC LIMIT 1
                """,
                (source, endpoint),
            )
        else:
            cur.execute(
                """
                SELECT payload_id, body, fetched_at FROM raw.payload
                WHERE source = %s AND endpoint = %s AND request_params->>'ticker' = %s
                ORDER BY fetched_at DESC LIMIT 1
                """,
                (source, endpoint, ticker),
            )
        row = cur.fetchone()
        return (row[0], bytes(row[1]), row[2]) if row else None


def run_parse(
    conn: psycopg.Connection, metric_map: dict[str, MetricMapping], tickers: list[str]
) -> tuple[ParseSummary, list[ParsedFact], dict[str, str]]:
    """Parse every ticker's latest EDGAR companyfacts payload into stg, and
    upsert core.entity / core.entity_ticker.

    Returns the summary, the parsed facts (for the caller to build a
    coverage report from), and a cik->ticker map (also for the report).
    """
    ticker_map_payload = latest_payload(conn, source="edgar", endpoint="ticker_map")
    if ticker_map_payload is None:
        raise RuntimeError(
            "no EDGAR ticker_map payload in raw.payload - run "
            "`pdw ingest --source edgar` first"
        )
    _, ticker_map_body, ticker_map_fetched_at = ticker_map_payload
    cik_by_ticker = parse_ticker_map(ticker_map_body)

    all_facts: list[ParsedFact] = []
    ticker_by_cik: dict[str, str] = {}
    entity_names_by_cik: dict[str, str] = {}
    tickers_without_cik: list[str] = []
    tickers_without_companyfacts: list[str] = []

    for ticker in tickers:
        cik = cik_by_ticker.get(normalize_ticker_for_vendor(ticker))
        if cik is None:
            tickers_without_cik.append(ticker)
            continue

        payload = latest_payload(conn, source="edgar", endpoint="companyfacts", ticker=ticker)
        if payload is None:
            tickers_without_companyfacts.append(ticker)
            continue

        payload_id, body, _ = payload
        facts = parse_companyfacts(body, metric_map, payload_id)
        all_facts.extend(facts)
        ticker_by_cik[cik] = ticker
        # entityName is a top-level attribute of the companyfacts response,
        # present even when an entity has zero parseable metric facts (e.g.
        # a freshly reorganized holding-company CIK with a filing but no
        # XBRL financials yet - verified live for XOM, 2026-07-20). Entity/
        # ticker mapping is a CLAUDE.md M3 deliverable in its own right, not
        # conditional on fundamentals existing.
        entity_names_by_cik[cik] = _extract_entity_name(body)

    with conn.cursor() as cur:
        cur.execute("TRUNCATE stg.edgar_fundamental_fact")
        cur.executemany(
            """
            INSERT INTO stg.edgar_fundamental_fact
                (cik, entity_name, metric_code, period_start, period_end,
                 fiscal_year, fiscal_period, value, unit, vendor_native_tag,
                 form_type, accession_no, filed_date, payload_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    f.cik,
                    f.entity_name,
                    f.metric_code,
                    f.period_start,
                    f.period_end,
                    f.fiscal_year,
                    f.fiscal_period,
                    f.value,
                    f.unit,
                    f.vendor_native_tag,
                    f.form_type,
                    f.accession_no,
                    f.filed_date,
                    f.payload_id,
                )
                for f in all_facts
            ],
        )

    _upsert_entities_and_tickers(conn, ticker_by_cik, entity_names_by_cik, ticker_map_fetched_at)
    conn.commit()

    summary = ParseSummary(
        entities_parsed=len(ticker_by_cik),
        facts_written=len(all_facts),
        tickers_without_cik=tuple(tickers_without_cik),
        tickers_without_companyfacts=tuple(tickers_without_companyfacts),
    )
    return summary, all_facts, ticker_by_cik


def _upsert_entities_and_tickers(
    conn: psycopg.Connection,
    ticker_by_cik: dict[str, str],
    entity_names_by_cik: dict[str, str],
    knowledge_from: datetime,
) -> None:
    """Insert new entities, refresh their name, and open/close ticker
    mappings as needed (CLAUDE.md 5: entity_ticker is bitemporal).

    `knowledge_from` is when we fetched the ticker map, not a true historical
    reassignment date - SEC's map is current-state-only (docs/limitations.md).
    """
    for cik, ticker in ticker_by_cik.items():
        name = entity_names_by_cik.get(cik)
        if name is None:
            continue

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO core.entity (cik, name) VALUES (%s, %s)
                ON CONFLICT (cik) DO UPDATE SET name = EXCLUDED.name
                RETURNING entity_id
                """,
                (cik, name),
            )
            row = cur.fetchone()
            assert row is not None
            entity_id: int = row[0]

            cur.execute(
                """
                SELECT ticker FROM core.entity_ticker
                WHERE entity_id = %s AND knowledge_to = 'infinity'
                """,
                (entity_id,),
            )
            open_row = cur.fetchone()

            if open_row is None:
                cur.execute(
                    """
                    INSERT INTO core.entity_ticker (entity_id, ticker, knowledge_from)
                    VALUES (%s, %s, %s)
                    """,
                    (entity_id, ticker, knowledge_from),
                )
            elif open_row[0] != ticker:
                cur.execute(
                    """
                    UPDATE core.entity_ticker SET knowledge_to = %s
                    WHERE entity_id = %s AND knowledge_to = 'infinity'
                    """,
                    (knowledge_from, entity_id),
                )
                cur.execute(
                    """
                    INSERT INTO core.entity_ticker (entity_id, ticker, knowledge_from)
                    VALUES (%s, %s, %s)
                    """,
                    (entity_id, ticker, knowledge_from),
                )
            # else: this ticker is already the open mapping for this entity - no-op.
