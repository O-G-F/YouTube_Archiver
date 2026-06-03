"""unique collection_item youtube_video_id

Revision ID: 80918c2b556b
Revises: 9ca01b76fcad
Create Date: 2026-06-03 05:17:29.586580
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '80918c2b556b'
down_revision: Union[str, None] = '9ca01b76fcad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defensively remove any pre-existing duplicates (keep the lowest id per
    # collection/video). NULL youtube_video_id rows are left alone (NULLs are
    # allowed to repeat under the unique constraint).
    op.execute(
        "DELETE FROM collection_items "
        "WHERE youtube_video_id IS NOT NULL AND id NOT IN ("
        "  SELECT min_id FROM ("
        "    SELECT MIN(id) AS min_id FROM collection_items "
        "    WHERE youtube_video_id IS NOT NULL "
        "    GROUP BY collection_id, youtube_video_id"
        "  ) AS keep"
        ")"
    )
    with op.batch_alter_table("collection_items") as batch_op:
        batch_op.create_unique_constraint(
            "uq_collection_youtube_video", ["collection_id", "youtube_video_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("collection_items") as batch_op:
        batch_op.drop_constraint("uq_collection_youtube_video", type_="unique")
