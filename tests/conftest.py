from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from pdw.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DB_NAME = "pdw_test"


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    """A throwaway Postgres database, migrated once per test session.

    DB-touching tests must never write to the same database a developer runs
    `make migrate`/`pdw ingest` against — raw.payload is append-only (no
    DELETE, by design), so there would be no way to clean up test rows
    afterwards. Instead this creates a separate `pdw_test` database on the
    same server, migrates it, and drops it whole at the end of the session.
    """
    base_url = get_settings().database_url
    server_url, _, _ = base_url.rpartition("/")
    admin_url = f"{server_url}/postgres"
    test_url = f"{server_url}/{TEST_DB_NAME}"

    with psycopg.connect(admin_url, autocommit=True) as admin_conn, admin_conn.cursor() as cur:
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(TEST_DB_NAME)))
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB_NAME)))

    env = os.environ.copy()
    env["PDW_DATABASE_URL"] = test_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=PROJECT_ROOT,
        env=env,
    )

    yield test_url

    with psycopg.connect(admin_url, autocommit=True) as admin_conn, admin_conn.cursor() as cur:
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(TEST_DB_NAME)))


@pytest.fixture
def db_connection(test_database_url: str) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(test_database_url)
    try:
        yield conn
    finally:
        conn.close()
