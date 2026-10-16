"""Per-camera recording profile and incident continuity measurements.

Revision ID: 0012_recording_quality
Revises: 0011_incidents
Create Date: 2026-08-27

Three additive changes, none of which rewrites existing footage or index rows:

* ``cameras.recording_profile`` -- how a camera's bitstream is handled. Every
  existing row becomes ``auto``, which resolves to the previous plain stream
  copy until Sentinel actually measures corruption on that camera, so an
  upgrade changes no FFmpeg command by itself.
* ``camera_incidents.coverage_ratio`` / ``damaged_segments`` -- the continuity
  measurement behind a new incident. Nullable and defaulted, so incidents
  written before this revision stay valid and readable.
* Three new ``camera_incidents.reason`` values. The column is a non-native
  ``Enum``, which SQLite implements as a VARCHAR CHECK constraint, so the
  constraint has to be rebuilt in batch mode for the new members to be
  accepted at all.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_recording_quality"
down_revision: str | None = "0011_incidents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_REASONS = (
    "recording_not_fresh",
    "recorder_restarting",
    "recorder_unavailable",
    "stream_source_failure",
    "motion_worker_unavailable",
    "relay_unavailable",
    "storage_unavailable",
)
_NEW_REASONS = (
    *_OLD_REASONS,
    "recording_coverage_low",
    "recording_unstable",
    "stream_corruption",
)

_PROFILES = ("auto", "passthrough", "resilient", "normalised")


def _reason_enum(values: Sequence[str]) -> sa.Enum:
    return sa.Enum(*values, name="incidentreason", native_enum=False, length=32)


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column(
            "recording_profile",
            sa.Enum(*_PROFILES, name="recordingprofile", native_enum=False, length=16),
            nullable=False,
            server_default="auto",
        ),
    )

    op.add_column("camera_incidents", sa.Column("coverage_ratio", sa.Float(), nullable=True))
    op.add_column(
        "camera_incidents",
        sa.Column("damaged_segments", sa.Integer(), nullable=False, server_default="0"),
    )

    # SQLite cannot widen a CHECK constraint in place; batch mode rebuilds the
    # table around it. Existing rows all carry one of the old values, so every
    # one of them still satisfies the widened constraint.
    with op.batch_alter_table("camera_incidents") as batch:
        batch.alter_column(
            "reason",
            existing_type=_reason_enum(_OLD_REASONS),
            type_=_reason_enum(_NEW_REASONS),
            existing_nullable=False,
        )


def downgrade() -> None:
    # Rows carrying a reason this revision introduced would violate the
    # narrowed constraint, so they are retired to the closest older meaning
    # rather than deleted: an incident is a record that something happened, and
    # a downgrade is not a reason to lose it.
    op.execute(
        sa.text(
            "UPDATE camera_incidents SET reason = 'recording_not_fresh' "
            "WHERE reason IN ('recording_coverage_low', 'recording_unstable')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE camera_incidents SET reason = 'stream_source_failure' "
            "WHERE reason = 'stream_corruption'"
        )
    )

    with op.batch_alter_table("camera_incidents") as batch:
        batch.alter_column(
            "reason",
            existing_type=_reason_enum(_NEW_REASONS),
            type_=_reason_enum(_OLD_REASONS),
            existing_nullable=False,
        )

    op.drop_column("camera_incidents", "damaged_segments")
    op.drop_column("camera_incidents", "coverage_ratio")
    op.drop_column("cameras", "recording_profile")
