"""initial schema: five schemas + ops.pipeline_run

Revision ID: 0001
Revises:
Create Date: 2026-07-19

"""

from __future__ import annotations

from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def upgrade() -> None:
    # Raw multi-statement DDL, executed verbatim via the DBAPI directly (not
    # through SQLAlchemy's text()) so no bind-parameter parsing is applied to the
    # hand-written SQL. Migrations are SQL-only revisions.
    sql = (SQL_DIR / "0001_initial_schema.sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    sql = (SQL_DIR / "0001_initial_schema_downgrade.sql").read_text()
    op.get_bind().exec_driver_sql(sql)
