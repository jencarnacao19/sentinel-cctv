"""Store ONVIF PTZ capabilities per camera.

Revision ID: 0007_camera_ptz
Revises: 0006_saved_exports
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_camera_ptz"
down_revision: str | None = "0006_saved_exports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cameras", sa.Column("onvif_device_xaddr", sa.String(2048), nullable=True))
    op.add_column("cameras", sa.Column("onvif_ptz_xaddr", sa.String(2048), nullable=True))
    op.add_column("cameras", sa.Column("onvif_profile_token", sa.String(512), nullable=True))
    op.add_column("cameras", sa.Column("ptz_supported", sa.Boolean(), nullable=True))
    op.add_column(
        "cameras",
        sa.Column("ptz_zoom_supported", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("cameras", sa.Column("ptz_status_message", sa.Text(), nullable=True))
    op.add_column("cameras", sa.Column("ptz_last_checked_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("cameras", "ptz_last_checked_at")
    op.drop_column("cameras", "ptz_status_message")
    op.drop_column("cameras", "ptz_zoom_supported")
    op.drop_column("cameras", "ptz_supported")
    op.drop_column("cameras", "onvif_profile_token")
    op.drop_column("cameras", "onvif_ptz_xaddr")
    op.drop_column("cameras", "onvif_device_xaddr")
