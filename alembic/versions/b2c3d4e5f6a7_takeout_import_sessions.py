"""takeout import sessions (Phase 6C)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-07 02:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Count columns are NOT NULL with a server_default so the table is portable
    # across PostgreSQL and SQLite. Only the ZIP basename + aggregate counts are
    # stored — never the full path, raw_json, or personal history rows.
    op.create_table(
        "takeout_import_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=40), nullable=False),
        sa.Column("path_basename", sa.String(length=255), nullable=True),
        sa.Column("source_kind", sa.String(length=48), nullable=True),
        sa.Column("import_kind", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="success"),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("scanned", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("imported", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skipped_duplicate", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_takeout_import_sessions_session_id"), "takeout_import_sessions", ["session_id"], unique=True)
    op.create_index(op.f("ix_takeout_import_sessions_import_kind"), "takeout_import_sessions", ["import_kind"], unique=False)
    op.create_index(op.f("ix_takeout_import_sessions_started_at"), "takeout_import_sessions", ["started_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_takeout_import_sessions_started_at"), table_name="takeout_import_sessions")
    op.drop_index(op.f("ix_takeout_import_sessions_import_kind"), table_name="takeout_import_sessions")
    op.drop_index(op.f("ix_takeout_import_sessions_session_id"), table_name="takeout_import_sessions")
    op.drop_table("takeout_import_sessions")
