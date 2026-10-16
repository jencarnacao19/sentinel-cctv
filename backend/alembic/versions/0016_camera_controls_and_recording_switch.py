"""Per-camera recording switch, explicit Tapo control opt-in, manual Tapo clips.

Revision ID: 0016_camera_controls
Revises: 0015_tapo_battery_provider
Create Date: 2026-08-28

Three additions, all defaulting to the behaviour that is already in place:

* ``cameras.recording_enabled`` starts ``true``, so every camera that records
  today keeps recording;
* ``cameras.tapo_controls_enabled`` starts ``NULL``, meaning "nobody has said" --
  Sentinel then decides from the camera's own manufacturer/model rather than
  offering a vendor's controls to every RTSP source on the network;
* ``segmentorigin`` gains ``tapo_manual`` so a bounded, explicitly requested
  battery-camera recording is distinguishable in the timeline from continuous
  footage and from an imported event clip.

The origin column is a non-native (VARCHAR + CHECK) enum, so widening it is a
constraint change rather than a type change.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_camera_controls"
down_revision: str | None = "0015_tapo_battery_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORIGIN_BEFORE = ("continuous", "tapo_event")
_ORIGIN_AFTER = ("continuous", "tapo_event", "tapo_manual")


def _origin_enum(values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name="segmentorigin", native_enum=False, length=16)


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column(
            "recording_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "cameras",
        sa.Column("tapo_controls_enabled", sa.Boolean(), nullable=True),
    )
    with op.batch_alter_table("recording_segments") as batch:
        batch.alter_column(
            "origin",
            existing_type=_origin_enum(_ORIGIN_BEFORE),
            type_=_origin_enum(_ORIGIN_AFTER),
            existing_nullable=False,
            existing_server_default="continuous",
        )


def downgrade() -> None:
    connection = op.get_bind()
    manual = connection.execute(
        sa.text("SELECT COUNT(*) FROM recording_segments WHERE origin = 'tapo_manual'")
    ).scalar_one()
    if manual:
        raise RuntimeError(
            "Cannot downgrade while manual Tapo battery recordings are indexed; "
            "those clips would become unreadable to the timeline. "
            "Remove or re-label them explicitly first."
        )

    with op.batch_alter_table("recording_segments") as batch:
        batch.alter_column(
            "origin",
            existing_type=_origin_enum(_ORIGIN_AFTER),
            type_=_origin_enum(_ORIGIN_BEFORE),
            existing_nullable=False,
            existing_server_default="continuous",
        )
    op.drop_column("cameras", "tapo_controls_enabled")
    op.drop_column("cameras", "recording_enabled")
