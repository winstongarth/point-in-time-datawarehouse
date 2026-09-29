"""stg.edgar_fundamental_fact + core.entity + core.entity_ticker

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-20

"""

from __future__ import annotations

from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def upgrade() -> None:
    sql = (SQL_DIR / "0003_stg_and_entity.sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    sql = (SQL_DIR / "0003_stg_and_entity_downgrade.sql").read_text()
    op.get_bind().exec_driver_sql(sql)
