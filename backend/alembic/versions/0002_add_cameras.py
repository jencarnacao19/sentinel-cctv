"""Add the cameras table.

Revision ID: 0002_cameras
Revises: 0001_initial
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.base import UTCDateTime

revision: str = "0002_cameras"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cameras",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=96), nullable=False),
        sa.Column("manufacturer", sa.String(length=96), nullable=True),
        sa.Column("model", sa.String(length=96), nullable=True),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("nvr_group", sa.String(length=96), nullable=True),
        sa.Column("main_stream_url", sa.String(length=2048), nullable=False),
        sa.Column("sub_stream_url", sa.String(length=2048), nullable=True),
        sa.Column("username", sa.String(length=128), nullable=True),
        sa.Column("encrypted_password", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "recording_mode",
            sa.Enum(
                "CONTINUOUS",
                "MOTION",
                "SCHEDULED",
                "OFF",
                name="recordingmode",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("motion_enabled", sa.Boolean(), nullable=False),
        sa.Column("motion_sensitivity", sa.Integer(), nullable=False),
        sa.Column("motion_zones", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "ONLINE",
                "OFFLINE",
                "CONNECTING",
                "ERROR",
                "DISABLED",
                name="camerastatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("status_message", sa.Text(), nullable=True),
        sa.Column("last_seen_at", UTCDateTime(), nullable=True),
        sa.Column("codec", sa.String(length=32), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("fps", sa.Float(), nullable=True),
        sa.Column("audio_codec", sa.String(length=32), nullable=True),
        sa.Column("bitrate_kbps", sa.Integer(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cameras")),
        sa.UniqueConstraint("name", name="uq_cameras_name"),
    )
    op.create_index("ix_cameras_enabled", "cameras", ["enabled"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cameras_enabled", table_name="cameras")
    op.drop_table("cameras")
