"""audit trail: append-only audit_events + audit_checkpoints (Phase 9E)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-05 00:00:00.000000

Downgrade drops both audit tables. Note: audit events are append-only evidence;
downgrading DESTROYS the audit trail — only do so on a throwaway/dev DB, never to
"clean up" production history. Retention pruning uses the app's cleanup (which
records a checkpoint), not a downgrade.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id_hash", sa.String(length=32), nullable=True),
        sa.Column("client_id_hash", sa.String(length=32), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("resource_type", sa.String(length=32), nullable=True),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=True),
        sa.Column("reason_code", sa.String(length=48), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("previous_hash", sa.String(length=64), nullable=True),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(op.f("ix_audit_events_occurred_at"), "audit_events", ["occurred_at"])
    op.create_index(op.f("ix_audit_events_event_type"), "audit_events", ["event_type"])
    op.create_index(op.f("ix_audit_events_category"), "audit_events", ["category"])
    op.create_index(op.f("ix_audit_events_severity"), "audit_events", ["severity"])
    op.create_index(op.f("ix_audit_events_request_id"), "audit_events", ["request_id"])
    op.create_index(op.f("ix_audit_events_correlation_id"), "audit_events", ["correlation_id"])
    op.create_index(op.f("ix_audit_events_event_hash"), "audit_events", ["event_hash"])

    op.create_table(
        "audit_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("up_to_event_id", sa.Integer(), nullable=False),
        sa.Column("boundary_hash", sa.String(length=64), nullable=True),
        sa.Column("deleted_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("audit_checkpoints")
    for ix in ("event_hash", "correlation_id", "request_id", "severity",
               "category", "event_type", "occurred_at"):
        op.drop_index(op.f(f"ix_audit_events_{ix}"), table_name="audit_events")
    op.drop_table("audit_events")
