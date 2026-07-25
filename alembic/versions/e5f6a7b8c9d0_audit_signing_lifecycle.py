"""audit signing lifecycle: event signing metadata + checkpoint boundaries (Phase 9E.1)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-05 02:00:00.000000

Adds per-event signing metadata (chain_version / signature_scheme / signing_key_id)
and extends audit_checkpoints with signing-boundary fields (checkpoint_type,
previous/next event + key ids, checkpoint_hash). Existing events default to the
legacy segment (chain_version=1, sha256_unsigned, key id 'legacy'); a subsequent
explicit signing/restore checkpoint bounds them — existing rows are never rewritten
or re-signed here. Downgrade drops the added columns (does not delete audit rows).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("chain_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("audit_events", sa.Column("signature_scheme", sa.String(length=20),
                                            nullable=False, server_default="sha256_unsigned"))
    op.add_column("audit_events", sa.Column("signing_key_id", sa.String(length=32),
                                            nullable=False, server_default="legacy"))

    op.add_column("audit_checkpoints", sa.Column("occurred_at", sa.DateTime(), nullable=True))
    op.add_column("audit_checkpoints", sa.Column("checkpoint_type", sa.String(length=24),
                                                 nullable=False, server_default="retention"))
    op.add_column("audit_checkpoints", sa.Column("reason_code", sa.String(length=48), nullable=True))
    op.add_column("audit_checkpoints", sa.Column("previous_event_id", sa.Integer(), nullable=True))
    op.add_column("audit_checkpoints", sa.Column("previous_event_hash", sa.String(length=64), nullable=True))
    op.add_column("audit_checkpoints", sa.Column("next_event_id", sa.Integer(), nullable=True))
    op.add_column("audit_checkpoints", sa.Column("previous_signing_key_id", sa.String(length=32), nullable=True))
    op.add_column("audit_checkpoints", sa.Column("next_signing_key_id", sa.String(length=32), nullable=True))
    op.add_column("audit_checkpoints", sa.Column("checkpoint_hash", sa.String(length=64), nullable=True))
    # signing-lifecycle checkpoints don't set the retention-only columns -> relax them
    op.alter_column("audit_checkpoints", "up_to_event_id", existing_type=sa.Integer(), nullable=True)
    op.alter_column("audit_checkpoints", "reason", existing_type=sa.String(length=32),
                    nullable=True, server_default="")
    # existing retention checkpoints (if any): stamp occurred_at from created_at
    op.execute("UPDATE audit_checkpoints SET occurred_at = created_at WHERE occurred_at IS NULL")


def downgrade() -> None:
    for col in ("checkpoint_hash", "next_signing_key_id", "previous_signing_key_id", "next_event_id",
                "previous_event_hash", "previous_event_id", "reason_code", "checkpoint_type", "occurred_at"):
        op.drop_column("audit_checkpoints", col)
    for col in ("signing_key_id", "signature_scheme", "chain_version"):
        op.drop_column("audit_events", col)
