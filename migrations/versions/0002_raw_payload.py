"""raw.payload: immutable landing zone + append-only trigger

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-20

"""

from __future__ import annotations

from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def upgrade() -> None:
    sql = (SQL_DIR / "0002_raw_payload.sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    sql = (SQL_DIR / "0002_raw_payload_downgrade.sql").read_text()
    op.get_bind().exec_driver_sql(sql)
