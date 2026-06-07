"""takeout import session job/benchmark fields (Phase 6D)

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-07 04:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("takeout_import_sessions", sa.Column("job_id", sa.Integer(), nullable=True))
    op.add_column("takeout_import_sessions", sa.Column("rq_job_id", sa.String(length=64), nullable=True))
    op.add_column("takeout_import_sessions", sa.Column("parser_backend", sa.String(length=16), nullable=True))
    op.add_column("takeout_import_sessions", sa.Column("entries_per_second", sa.Float(), nullable=True))
    op.add_column("takeout_import_sessions", sa.Column("peak_memory_mb", sa.Float(), nullable=True))
    op.add_column(
        "takeout_import_sessions",
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("takeout_import_sessions", sa.Column("current_phase", sa.String(length=32), nullable=True))
    op.add_column("takeout_import_sessions", sa.Column("last_update_at", sa.DateTime(), nullable=True))
    op.create_index(
        op.f("ix_takeout_import_sessions_job_id"), "takeout_import_sessions", ["job_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_takeout_import_sessions_job_id"), table_name="takeout_import_sessions")
    op.drop_column("takeout_import_sessions", "last_update_at")
    op.drop_column("takeout_import_sessions", "current_phase")
    op.drop_column("takeout_import_sessions", "cancel_requested")
    op.drop_column("takeout_import_sessions", "peak_memory_mb")
    op.drop_column("takeout_import_sessions", "entries_per_second")
    op.drop_column("takeout_import_sessions", "parser_backend")
    op.drop_column("takeout_import_sessions", "rq_job_id")
    op.drop_column("takeout_import_sessions", "job_id")
