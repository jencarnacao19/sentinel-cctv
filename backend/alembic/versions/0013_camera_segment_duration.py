"""Optional per-camera recording segment duration.

Revision ID: 0013_camera_segment_duration
Revises: 0012_recording_quality
Create Date: 2026-08-27

The nullable value inherits the existing system-wide duration, so upgrading
does not restart cameras with a different command or rewrite any footage.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_camera_segment_duration"
down_revision: str | None = "0012_recording_quality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_cameras_recording_segment_seconds"


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column("recording_segment_seconds", sa.Integer(), nullable=True),
    )
    with op.batch_alter_table("cameras") as batch:
        batch.create_check_constraint(
            op.f(_CONSTRAINT),
            "recording_segment_seconds IS NULL OR recording_segment_seconds BETWEEN 10 AND 300",
        )


def downgrade() -> None:
    with op.batch_alter_table("cameras") as batch:
        batch.drop_constraint(op.f(_CONSTRAINT), type_="check")
    op.drop_column("cameras", "recording_segment_seconds")
