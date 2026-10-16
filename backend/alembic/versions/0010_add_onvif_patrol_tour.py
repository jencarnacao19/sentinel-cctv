"""Remember the camera-owned ONVIF patrol tour.

Revision ID: 0010_onvif_patrol
Revises: 0009_tapo_password
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_onvif_patrol"
down_revision: str | None = "0009_tapo_password"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column("onvif_patrol_tour_token", sa.String(512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cameras", "onvif_patrol_tour_token")
