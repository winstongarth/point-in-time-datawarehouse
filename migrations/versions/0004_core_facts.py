"""core.fundamental_fact + core.price_fact: the bitemporal core

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-20

"""

from __future__ import annotations

from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def upgrade() -> None:
    sql = (SQL_DIR / "0004_core_facts.sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    sql = (SQL_DIR / "0004_core_facts_downgrade.sql").read_text()
    op.get_bind().exec_driver_sql(sql)
