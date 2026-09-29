"""dq.check_result + dq.exception

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-20

"""

from __future__ import annotations

from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def upgrade() -> None:
    sql = (SQL_DIR / "0005_dq.sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    sql = (SQL_DIR / "0005_dq_downgrade.sql").read_text()
    op.get_bind().exec_driver_sql(sql)
