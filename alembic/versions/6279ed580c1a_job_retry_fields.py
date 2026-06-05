"""job retry fields

Revision ID: 6279ed580c1a
Revises: b4c1b3203886
Create Date: 2026-06-05 04:39:29.969704
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6279ed580c1a'
down_revision: Union[str, None] = 'b4c1b3203886'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # retry_count is NOT NULL -> server_default so existing rows get 0.
    # retry_of_job_id is an informational self-reference; we add it as a plain
    # column (no ALTER-TABLE-ADD-FK, which SQLite cannot do) for portability.
    op.add_column('jobs', sa.Column('retry_count', sa.Integer(), nullable=False, server_default=sa.text('0')))
    op.add_column('jobs', sa.Column('retry_of_job_id', sa.Integer(), nullable=True))
    op.add_column('jobs', sa.Column('next_retry_at', sa.DateTime(), nullable=True))
    op.create_index(op.f('ix_jobs_next_retry_at'), 'jobs', ['next_retry_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_jobs_next_retry_at'), table_name='jobs')
    op.drop_column('jobs', 'next_retry_at')
    op.drop_column('jobs', 'retry_of_job_id')
    op.drop_column('jobs', 'retry_count')
