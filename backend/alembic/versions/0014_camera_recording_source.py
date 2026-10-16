"""Optional per-camera recorder source pin.

Revision ID: 0014_camera_recording_source
Revises: 0013_camera_segment_duration
Create Date: 2026-08-27

Existing cameras remain ``auto``. An administrator can pin one measured problem
camera to the shared local relay or its direct RTSP source without changing the
failover policy for any other camera.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_camera_recording_source"
down_revision: str | None = "0013_camera_segment_duration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODES = ("auto", "relay", "direct")


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column(
            "recording_source_mode",
            sa.Enum(*_MODES, name="recordingsourcemode", native_enum=False, length=8),
            nullable=False,
            server_default="auto",
        ),
    )


def downgrade() -> None:
    op.drop_column("cameras", "recording_source_mode")
