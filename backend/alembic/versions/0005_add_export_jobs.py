"""Add the export_jobs table.

Revision ID: 0005_exports
Revises: 0004_motion
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.database.base import UTCDateTime

revision: str = "0005_exports"
down_revision: str | None = "0004_motion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "export_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("camera_id", sa.String(length=36), nullable=False),
        sa.Column("start_at", UTCDateTime(), nullable=False),
        sa.Column("end_at", UTCDateTime(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "complete",
                "failed",
                "expired",
                "cancelled",
                name="exportstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=True),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.Column("expires_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_export_created", "export_jobs", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_export_created", table_name="export_jobs")
    op.drop_table("export_jobs")
