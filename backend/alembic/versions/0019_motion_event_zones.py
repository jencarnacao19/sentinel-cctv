"""Record which named detection zones a motion event covered.

Revision ID: 0019_motion_event_zones
Revises: 0018_scoped_api_tokens
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_motion_event_zones"
down_revision: str | None = "0018_scoped_api_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # JSON list of {"id", "name"}; the name is kept as it was at the time, so
    # renaming a zone later does not rewrite what an old event says.
    op.add_column("motion_events", sa.Column("zones", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("motion_events") as batch:
        batch.drop_column("zones")
