"""Add the recording_segments table.

Revision ID: 0003_segments
Revises: 0002_cameras
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.base import UTCDateTime

revision: str = "0003_segments"
down_revision: str | None = "0002_cameras"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recording_segments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("camera_id", sa.String(length=36), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=False),
        sa.Column("ended_at", UTCDateTime(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("codec", sa.String(length=32), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("fps", sa.Float(), nullable=True),
        sa.Column("has_motion", sa.Boolean(), nullable=False),
        sa.Column("motion_seconds", sa.Float(), nullable=False),
        sa.Column("protected", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "RECORDING",
                "COMPLETE",
                "DAMAGED",
                "MISSING",
                name="segmentstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["camera_id"],
            ["cameras.id"],
            name=op.f("fk_recording_segments_camera_id_cameras"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recording_segments")),
        sa.UniqueConstraint("path", name="uq_recording_segments_path"),
    )
    op.create_index("ix_segments_camera_started", "recording_segments", ["camera_id", "started_at"])
    op.create_index("ix_segments_started", "recording_segments", ["started_at"])
    op.create_index("ix_segments_protected", "recording_segments", ["protected"])


def downgrade() -> None:
    op.drop_index("ix_segments_protected", table_name="recording_segments")
    op.drop_index("ix_segments_started", table_name="recording_segments")
    op.drop_index("ix_segments_camera_started", table_name="recording_segments")
    op.drop_table("recording_segments")
