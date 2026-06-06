"""scheduler runs (Phase 7E)

Revision ID: a1b2c3d4e5f6
Revises: 6279ed580c1a
Create Date: 2026-06-07 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "6279ed580c1a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Integer count columns are NOT NULL with a server_default so the table is
    # portable across PostgreSQL and SQLite (no ALTER-ADD-NOT-NULL surprises).
    op.create_table(
        "scheduler_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("run_type", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="success"),
        sa.Column("selected_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("jobs_created", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("jobs_submitted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skipped_active_jobs", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skipped_duplicates", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skipped_backoff", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("retryable_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("partial_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("body_count_before", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("body_count_after", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_scheduler_runs_run_id"), "scheduler_runs", ["run_id"], unique=True)
    op.create_index(op.f("ix_scheduler_runs_run_type"), "scheduler_runs", ["run_type"], unique=False)
    op.create_index(op.f("ix_scheduler_runs_started_at"), "scheduler_runs", ["started_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_scheduler_runs_started_at"), table_name="scheduler_runs")
    op.drop_index(op.f("ix_scheduler_runs_run_type"), table_name="scheduler_runs")
    op.drop_index(op.f("ix_scheduler_runs_run_id"), table_name="scheduler_runs")
    op.drop_table("scheduler_runs")
