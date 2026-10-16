"""Add the camera_incidents table.

Revision ID: 0011_incidents
Revises: 0010_onvif_patrol
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.base import UTCDateTime

revision: str = "0011_incidents"
down_revision: str | None = "0010_onvif_patrol"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "camera_incidents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("camera_id", sa.String(length=36), nullable=False),
        sa.Column(
            "reason",
            sa.Enum(
                "recording_not_fresh",
                "recorder_restarting",
                "recorder_unavailable",
                "stream_source_failure",
                "motion_worker_unavailable",
                "relay_unavailable",
                "storage_unavailable",
                name="incidentreason",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("started_at", UTCDateTime(), nullable=False),
        sa.Column("resolved_at", UTCDateTime(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("detail", sa.String(length=240), nullable=True),
        sa.Column(
            "fault",
            sa.Enum(
                "missing_stream_parameters",
                "corrupt_packets",
                "connection_failed",
                "timeout",
                "authentication_rejected",
                "unknown",
                name="streamfault",
                native_enum=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column(
            "source",
            sa.Enum(
                "relay",
                "direct",
                name="recordersource",
                native_enum=False,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column("restart_count", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_camera_incidents_camera_started",
        "camera_incidents",
        ["camera_id", "started_at"],
        unique=False,
    )
    op.create_index("ix_camera_incidents_started", "camera_incidents", ["started_at"], unique=False)
    op.create_index(
        "ix_camera_incidents_resolved", "camera_incidents", ["resolved_at"], unique=False
    )
    op.create_index(
        "uq_camera_incidents_open_camera",
        "camera_incidents",
        ["camera_id"],
        unique=True,
        sqlite_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_camera_incidents_open_camera", table_name="camera_incidents")
    op.drop_index("ix_camera_incidents_resolved", table_name="camera_incidents")
    op.drop_index("ix_camera_incidents_started", table_name="camera_incidents")
    op.drop_index("ix_camera_incidents_camera_started", table_name="camera_incidents")
    op.drop_table("camera_incidents")
