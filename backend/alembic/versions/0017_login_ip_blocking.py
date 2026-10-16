"""Persistent public-login IP blocking.

Revision ID: 0017_login_ip_blocking
Revises: 0016_camera_controls
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_login_ip_blocking"
down_revision: str | None = "0016_camera_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_client_states",
        sa.Column("ip_address", sa.String(length=45), nullable=False),
        sa.Column("public_hostname", sa.String(length=253), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("first_failed_at", sa.DateTime(), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(), nullable=False),
        sa.Column("blocked_at", sa.DateTime(), nullable=True),
        sa.Column("blocked_until", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("failed_attempts >= 0", name="login_client_failed_attempts"),
        sa.PrimaryKeyConstraint("ip_address"),
    )
    op.create_index(
        "ix_login_client_states_blocked_at",
        "login_client_states",
        ["blocked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_login_client_states_blocked_at", table_name="login_client_states")
    op.drop_table("login_client_states")
