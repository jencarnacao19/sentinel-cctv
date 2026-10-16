"""Protect saved clip exports from automatic expiry.

Revision ID: 0006_saved_exports
Revises: 0005_exports
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_saved_exports"
down_revision: str | None = "0005_exports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing exports keep their old 24-hour behaviour. New clips created by
    # the API opt into protection explicitly.
    op.add_column(
        "export_jobs",
        sa.Column("protected", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_export_protected", "export_jobs", ["protected"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_export_protected", table_name="export_jobs")
    op.drop_column("export_jobs", "protected")
