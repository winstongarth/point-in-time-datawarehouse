from __future__ import annotations

from pathlib import Path

import psycopg

from pdw.dictionary import generate_dictionary


def test_generates_a_file_per_known_table(
    db_connection: psycopg.Connection, tmp_path: Path
) -> None:
    written = generate_dictionary(db_connection, tmp_path)

    names = {p.name for p in written}
    assert "core.fundamental_fact.md" in names
    assert "core.price_fact.md" in names
    assert "raw.payload.md" in names
    assert "dq.exception.md" in names
    assert "ops.pipeline_run.md" in names


def test_regenerates_deterministically(db_connection: psycopg.Connection, tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    generate_dictionary(db_connection, first_dir)
    generate_dictionary(db_connection, second_dir)

    first_contents = {p.name: p.read_text() for p in first_dir.iterdir()}
    second_contents = {p.name: p.read_text() for p in second_dir.iterdir()}

    assert first_contents == second_contents


def test_fundamental_fact_dictionary_documents_bitemporal_columns(
    db_connection: psycopg.Connection, tmp_path: Path
) -> None:
    generate_dictionary(db_connection, tmp_path)

    text = (tmp_path / "core.fundamental_fact.md").read_text()

    assert "knowledge_from" in text
    assert "knowledge_to" in text
    assert "supersedes" in text
    assert "restates" in text  # the curated note on the supersedes column
