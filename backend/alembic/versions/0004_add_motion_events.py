"""Add the motion_events table.

Revision ID: 0004_motion
Revises: 0003_segments
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.base import UTCDateTime

revision: str = "0004_motion"
down_revision: str | None = "0003_segments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "motion_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("camera_id", sa.String(length=36), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=False),
        sa.Column("ended_at", UTCDateTime(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("peak_score", sa.Float(), nullable=False),
        sa.Column("pre_roll_start", UTCDateTime(), nullable=False),
        sa.Column("post_roll_end", UTCDateTime(), nullable=True),
        sa.Column("snapshot_path", sa.String(length=1024), nullable=True),
        sa.Column("thumbnail_path", sa.String(length=1024), nullable=True),
        sa.Column("protected", sa.Boolean(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["camera_id"],
            ["cameras.id"],
            name=op.f("fk_motion_events_camera_id_cameras"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_motion_events")),
    )
    op.create_index("ix_motion_camera_started", "motion_events", ["camera_id", "started_at"])
    op.create_index("ix_motion_started", "motion_events", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_motion_started", table_name="motion_events")
    op.drop_index("ix_motion_camera_started", table_name="motion_events")
    op.drop_table("motion_events")
