from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import psycopg

from pdw.availability import SourceAvailability, compute_knowledge_from

logger = logging.getLogger(__name__)

SOURCE = "edgar"


@dataclass(frozen=True)
class ChainLink:
    """One genuine version of a (entity, metric, period) fact - a value that
    was actually new when it was filed, not just a routine re-report of an
    already-known figure (unchanged facts are no-ops)."""

    accession_no: str
    filed_date: date
    value: object
    unit: str
    vendor_native_tag: str | None
    form_type: str | None
    fiscal_year: int | None
    fiscal_period: str | None
    payload_id: int
    knowledge_from: datetime


@dataclass
class LoadSummary:
    keys_processed: int = 0
    rows_inserted: int = 0
    rows_relinked: int = 0


def load_fundamentals(conn: psycopg.Connection, availability: SourceAvailability) -> LoadSummary:
    """Promote stg.edgar_fundamental_fact into core.fundamental_fact.

    For each (cik, metric_code, period_start, period_end) key, the true
    restatement chain is recomputed from scratch every run (stg holds full
    history each time, since EDGAR's companyfacts response always does) and
    reconciled against whatever core.fundamental_fact already has: missing
    versions are inserted, existing ones have their knowledge_to/supersedes
    corrected if a newly-discovered earlier or later version changes the
    chain (handles out-of-order arrival). Recomputing and reconciling,
    rather than only ever appending, is what makes a second run with
    unchanged input a genuine no-op.

    period_start is part of the key, not just period_end - a single
    accession can report more than one duration ending on the same date
    (e.g. a 10-Q's 3-month and 6-month revenue figures both end June 30),
    and those are different, simultaneously-true facts, not restatements
    of each other.
    """
    summary = LoadSummary()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT cik, metric_code, period_start, period_end
            FROM stg.edgar_fundamental_fact
            WHERE cik IS NOT NULL AND metric_code IS NOT NULL AND period_end IS NOT NULL
            """
        )
        keys = cur.fetchall()

    for cik, metric_code, period_start, period_end in keys:
        entity_id = _get_entity_id(conn, cik)
        if entity_id is None:
            logger.warning(
                "no core.entity for cik, skipping fundamentals load",
                extra={"cik": cik, "metric_code": metric_code},
            )
            continue

        rows = _fetch_stg_versions(conn, cik, metric_code, period_start, period_end)
        chain = _build_chain(rows, availability.availability_lag_days)
        if not chain:
            continue

        _reconcile(conn, entity_id, metric_code, period_start, period_end, chain, summary)
        summary.keys_processed += 1

    conn.commit()
    return summary


def _get_entity_id(conn: psycopg.Connection, cik: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SELECT entity_id FROM core.entity WHERE cik = %s", (cik,))
        row = cur.fetchone()
        return row[0] if row else None


def _fetch_stg_versions(
    conn: psycopg.Connection,
    cik: str,
    metric_code: str,
    period_start: date | None,
    period_end: date,
) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT accession_no, filed_date, value, unit, vendor_native_tag,
                   form_type, fiscal_year, fiscal_period, payload_id
            FROM stg.edgar_fundamental_fact
            WHERE cik = %s AND metric_code = %s AND period_end = %s
                  AND period_start IS NOT DISTINCT FROM %s
                  AND accession_no IS NOT NULL AND filed_date IS NOT NULL
            """,
            (cik, metric_code, period_end, period_start),
        )
        return cur.fetchall()


def _build_chain(rows: list[tuple[Any, ...]], lag_days: int) -> list[ChainLink]:
    # Collapse to one row per accession_no (stg may hold near-duplicates -
    # e.g. the same accession reported via more than one raw.payload fetch).
    by_accession: dict[str, tuple[Any, ...]] = {}
    for row in rows:
        accession_no, filed_date = row[0], row[1]
        existing = by_accession.get(accession_no)
        if existing is None or filed_date < existing[1]:
            by_accession[accession_no] = row

    ordered = sorted(by_accession.values(), key=lambda r: (r[1], r[0]))

    chain: list[ChainLink] = []
    current_value: object = _NO_VALUE
    current_unit: str | None = None
    for row in ordered:
        (
            accession_no,
            filed_date,
            value,
            unit,
            vendor_native_tag,
            form_type,
            fiscal_year,
            fiscal_period,
            payload_id,
        ) = row
        if value == current_value and unit == current_unit:
            continue  # a routine re-report of an already-known value, not a restatement
        current_value, current_unit = value, unit

        knowledge_from = compute_knowledge_from(filed_date, lag_days)
        if chain and knowledge_from <= chain[-1].knowledge_from:
            # EDGAR's `filed` field is date-only: two distinct-valued
            # accessions filed on the same calendar date compute the same
            # knowledge_from, which would violate the strict knowledge_from
            # < knowledge_to ordering. Stagger by a second per position -
            # correct to within EDGAR's own granularity, and enough to keep
            # the chain strictly increasing.
            knowledge_from = chain[-1].knowledge_from + _ONE_SECOND

        chain.append(
            ChainLink(
                accession_no=accession_no,
                filed_date=filed_date,
                value=value,
                unit=unit,
                vendor_native_tag=vendor_native_tag,
                form_type=form_type,
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                payload_id=payload_id,
                knowledge_from=knowledge_from,
            )
        )
    return chain


_NO_VALUE = object()
_ONE_SECOND = timedelta(seconds=1)


def _reconcile(
    conn: psycopg.Connection,
    entity_id: int,
    metric_code: str,
    period_start: date | None,
    period_end: date,
    chain: list[ChainLink],
    summary: LoadSummary,
) -> None:
    existing = _existing_rows(conn, entity_id, metric_code, period_start, period_end)
    prev_fact_id: int | None = None

    for i, link in enumerate(chain):
        knowledge_to: object = chain[i + 1].knowledge_from if i + 1 < len(chain) else "infinity"
        row = existing.get(link.accession_no)

        if row is None:
            fact_id = _insert_fact(
                conn,
                entity_id,
                metric_code,
                period_start,
                period_end,
                link,
                knowledge_to,
                prev_fact_id,
            )
            summary.rows_inserted += 1
        else:
            fact_id = row[0]
            _relink_fact(conn, fact_id, knowledge_to, prev_fact_id)
            summary.rows_relinked += 1

        prev_fact_id = fact_id


def _existing_rows(
    conn: psycopg.Connection,
    entity_id: int,
    metric_code: str,
    period_start: date | None,
    period_end: date,
) -> dict[str, tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fact_id, accession_no
            FROM core.fundamental_fact
            WHERE entity_id = %s AND metric_code = %s AND period_end = %s
                  AND period_start IS NOT DISTINCT FROM %s AND source = %s
            """,
            (entity_id, metric_code, period_end, period_start, SOURCE),
        )
        return {row[1]: row for row in cur.fetchall()}


def _insert_fact(
    conn: psycopg.Connection,
    entity_id: int,
    metric_code: str,
    period_start: date | None,
    period_end: date,
    link: ChainLink,
    knowledge_to: object,
    supersedes: int | None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.fundamental_fact
                (entity_id, metric_code, period_start, period_end, fiscal_year,
                 fiscal_period, value, unit, source, vendor_native_tag, form_type,
                 accession_no, filed_date, knowledge_from, knowledge_to, supersedes,
                 payload_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::timestamptz, %s, %s)
            RETURNING fact_id
            """,
            (
                entity_id,
                metric_code,
                period_start,
                period_end,
                link.fiscal_year,
                link.fiscal_period,
                link.value,
                link.unit,
                SOURCE,
                link.vendor_native_tag,
                link.form_type,
                link.accession_no,
                link.filed_date,
                link.knowledge_from,
                knowledge_to,
                supersedes,
                link.payload_id,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        fact_id: int = row[0]
        return fact_id


def _relink_fact(
    conn: psycopg.Connection, fact_id: int, knowledge_to: object, supersedes: int | None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.fundamental_fact
            SET knowledge_to = %s::timestamptz, supersedes = %s
            WHERE fact_id = %s
            """,
            (knowledge_to, supersedes, fact_id),
        )
