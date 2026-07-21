from __future__ import annotations

from pathlib import Path

import psycopg

_SCHEMAS = ("raw", "stg", "core", "dq", "ops")

# Hand-curated: "source, transformation applied, known caveats" needs
# domain knowledge information_schema can't provide. Deliberately
# terse - only non-obvious columns get a note; everything else is fully
# described by its type/nullability alone. No wall-clock content anywhere
# in this module's output, so re-running against an unchanged schema
# produces byte-identical files (the accept criterion: "regenerates
# deterministically").

_TABLE_NOTES: dict[str, str] = {
    "raw.payload": (
        "Immutable landing zone. Every vendor response is stored verbatim; "
        "append-only, enforced by a `BEFORE UPDATE OR DELETE` trigger "
        "(invariant 6). Every `core` row traces back here via `payload_id`."
    ),
    "stg.edgar_fundamental_fact": (
        "Parsed EDGAR XBRL datapoints with the metric map already applied. "
        "Truncated and rebuilt on every `pdw parse` run; not deduplicated "
        "and has no constraints beyond column types, by design."
    ),
    "core.entity": "One row per company (by CIK), independent of ticker history.",
    "core.entity_ticker": (
        "Bitemporal ticker->entity mapping. A brand-new entity's first "
        "mapping is backdated to a fixed sentinel (2000-01-01 UTC), not the "
        "ingestion date - SEC's ticker map is current-state-only, so there "
        "is no true historical assignment date to recover regardless. "
        "A genuine reassignment, once detected, opens at real detection time."
    ),
    "core.fundamental_fact": (
        "The bitemporal core for the 6 tracked fundamental metrics. A "
        "restatement never updates a row - it closes the prior row's "
        "`knowledge_to` and inserts a successor with `supersedes` set."
    ),
    "core.price_fact": (
        "The bitemporal core for daily prices. Multiple `source`s "
        "(yfinance, tiingo) may hold independent, simultaneously-valid "
        "rows for the same entity/date - the cross-vendor reconciliation "
        "check reconciles them, this table doesn't merge them itself."
    ),
    "dq.check_result": (
        "One row per check per run, including passes - a check that only "
        "records failures cannot support a coverage metric."
    ),
    "dq.exception": (
        "The triage lifecycle (open -> triage -> closed) for a recurring "
        "failure, identified by `check_name` (via `check_id`) + "
        "`dimension_key` - separate from `check_result`'s per-run log."
    ),
    "ops.pipeline_run": (
        "One row per pipeline invocation. Every `raw.payload` and "
        "`dq.check_result` row traces back to a `run_id` here."
    ),
}

_COLUMN_NOTES: dict[str, dict[str, str]] = {
    "raw.payload": {
        "content_sha256": (
            "Indexed with `source` - lets downstream steps detect an unchanged fetch."
        ),
    },
    "core.entity_ticker": {
        "knowledge_from": "See table note - not always a true historical date.",
        "knowledge_to": "`infinity` means this ticker is the current mapping for the entity.",
    },
    "core.fundamental_fact": {
        "period_start": "NULL for instant concepts (e.g. Assets, StockholdersEquity).",
        "vendor_native_tag": (
            "The actual XBRL tag used - visible when a filer switches tags mid-history."
        ),
        "knowledge_to": "`infinity` means this is the currently-believed-true value.",
        "supersedes": "Points to the fact_id this row restates, if any.",
    },
    "core.price_fact": {
        "adj_close": (
            "Diverges from `close` after a split/dividend - the mechanism for "
            "detecting retroactive adjustment."
        ),
        "knowledge_to": "`infinity` means this is the currently-believed-true value.",
    },
    "dq.check_result": {
        "observed": "Actual values the check computed.",
        "expected": "The threshold/shape the check compared against.",
    },
    "dq.exception": {
        "dimension_key": (
            "Groups repeated failures of the same issue across runs "
            "(e.g. a ticker, or ticker+period)."
        ),
        "status": (
            "open -> triage (human-acknowledged) -> closed "
            "(manually or auto-resolved on a later pass)."
        ),
    },
}


def generate_dictionary(conn: psycopg.Connection, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for schema, table in _list_tables(conn):
        qualified = f"{schema}.{table}"
        columns = _list_columns(conn, schema, table)
        path = out_dir / f"{qualified}.md"
        path.write_text(_render_table(qualified, columns), encoding="utf-8")
        written.append(path)
    return sorted(written)


def _list_tables(conn: psycopg.Connection) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema = ANY(%s) AND table_type = 'BASE TABLE'
            ORDER BY table_schema, table_name
            """,
            (list(_SCHEMAS),),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def _list_columns(conn: psycopg.Connection, schema: str, table: str) -> list[tuple[str, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        return [(row[0], row[1], row[2]) for row in cur.fetchall()]


def _render_table(qualified_name: str, columns: list[tuple[str, str, str]]) -> str:
    lines = [f"# `{qualified_name}`", ""]
    table_note = _TABLE_NOTES.get(qualified_name)
    if table_note:
        lines += [table_note, ""]

    column_notes = _COLUMN_NOTES.get(qualified_name, {})
    lines += ["| Field | Type | Nullable | Notes |", "|---|---|---|---|"]
    for name, data_type, is_nullable in columns:
        nullable = "yes" if is_nullable == "YES" else "no"
        note = column_notes.get(name, "")
        lines.append(f"| `{name}` | {data_type} | {nullable} | {note} |")

    return "\n".join(lines) + "\n"
