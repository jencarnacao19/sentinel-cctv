"""Event-based Tapo battery-camera provider.

Revision ID: 0015_tapo_battery_provider
Revises: 0014_camera_recording_source
Create Date: 2026-08-27

Existing cameras and segments remain RTSP/continuous.  Battery cameras may
omit an RTSP URL and imported event clips are explicitly labelled, allowing
the ordinary timeline/playback code to consume them without pretending they
are continuous footage.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_tapo_battery_provider"
down_revision: str | None = "0014_camera_recording_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column(
            "kind",
            sa.Enum("rtsp", "tapo_battery", name="camerakind", native_enum=False, length=24),
            nullable=False,
            server_default="rtsp",
        ),
    )
    op.add_column("cameras", sa.Column("tapo_child_id", sa.String(length=128), nullable=True))
    with op.batch_alter_table("cameras") as batch:
        batch.alter_column(
            "main_stream_url",
            existing_type=sa.String(length=2048),
            nullable=True,
        )
        batch.alter_column(
            "recording_mode",
            existing_type=sa.Enum(
                "CONTINUOUS",
                "MOTION",
                "SCHEDULED",
                "OFF",
                name="recordingmode",
                native_enum=False,
                length=16,
            ),
            type_=sa.Enum(
                "CONTINUOUS",
                "MOTION",
                "SCHEDULED",
                "EVENTS",
                "OFF",
                name="recordingmode",
                native_enum=False,
                length=16,
            ),
            existing_nullable=False,
        )

    op.add_column(
        "recording_segments",
        sa.Column(
            "origin",
            sa.Enum(
                "continuous",
                "tapo_event",
                name="segmentorigin",
                native_enum=False,
                length=16,
            ),
            nullable=False,
            server_default="continuous",
        ),
    )
    op.add_column(
        "recording_segments",
        sa.Column("external_id", sa.String(length=160), nullable=True),
    )
    with op.batch_alter_table("recording_segments") as batch:
        batch.create_unique_constraint(
            "uq_recording_segments_camera_external",
            ["camera_id", "external_id"],
        )


def downgrade() -> None:
    connection = op.get_bind()
    battery_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM cameras WHERE kind = 'tapo_battery'")
    ).scalar_one()
    if battery_count:
        raise RuntimeError(
            "Cannot downgrade while Tapo battery cameras exist; remove them explicitly first."
        )

    with op.batch_alter_table("recording_segments") as batch:
        batch.drop_constraint("uq_recording_segments_camera_external", type_="unique")
    op.drop_column("recording_segments", "external_id")
    op.drop_column("recording_segments", "origin")

    with op.batch_alter_table("cameras") as batch:
        batch.alter_column(
            "recording_mode",
            existing_type=sa.Enum(
                "CONTINUOUS",
                "MOTION",
                "SCHEDULED",
                "EVENTS",
                "OFF",
                name="recordingmode",
                native_enum=False,
                length=16,
            ),
            type_=sa.Enum(
                "CONTINUOUS",
                "MOTION",
                "SCHEDULED",
                "OFF",
                name="recordingmode",
                native_enum=False,
                length=16,
            ),
            existing_nullable=False,
        )
        batch.alter_column(
            "main_stream_url",
            existing_type=sa.String(length=2048),
            nullable=False,
        )
    op.drop_column("cameras", "tapo_child_id")
    op.drop_column("cameras", "kind")
