"""Configure Tapo patrol restoration after manual PTZ.

Revision ID: 0008_tapo_patrol
Revises: 0007_camera_ptz
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_tapo_patrol"
down_revision: str | None = "0007_camera_ptz"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cameras", sa.Column("tapo_patrol_mode", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("cameras", "tapo_patrol_mode")
