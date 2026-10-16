"""Store an optional Tapo control password separately.

Revision ID: 0009_tapo_password
Revises: 0008_tapo_patrol
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_tapo_password"
down_revision: str | None = "0008_tapo_patrol"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cameras", sa.Column("encrypted_tapo_password", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("cameras", "encrypted_tapo_password")
