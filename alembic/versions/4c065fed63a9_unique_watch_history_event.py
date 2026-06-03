"""unique watch_history_event

Revision ID: 4c065fed63a9
Revises: 80918c2b556b
Create Date: 2026-06-03 09:36:11.804749
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4c065fed63a9'
down_revision: Union[str, None] = '80918c2b556b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defensively dedup existing rows (keep lowest id per source/video/time).
    op.execute(
        "DELETE FROM watch_history_events "
        "WHERE youtube_video_id IS NOT NULL AND watched_at IS NOT NULL AND id NOT IN ("
        "  SELECT min_id FROM ("
        "    SELECT MIN(id) AS min_id FROM watch_history_events "
        "    WHERE youtube_video_id IS NOT NULL AND watched_at IS NOT NULL "
        "    GROUP BY source, youtube_video_id, watched_at"
        "  ) AS keep"
        ")"
    )
    with op.batch_alter_table("watch_history_events") as batch_op:
        batch_op.create_unique_constraint(
            "uq_watch_event", ["source", "youtube_video_id", "watched_at"]
        )


def downgrade() -> None:
    with op.batch_alter_table("watch_history_events") as batch_op:
        batch_op.drop_constraint("uq_watch_event", type_="unique")
