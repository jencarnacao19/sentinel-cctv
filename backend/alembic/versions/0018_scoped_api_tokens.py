"""Limit integration tokens while preserving existing camera-control clients.

Revision ID: 0018_scoped_api_tokens
Revises: 0017_login_ip_blocking
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_scoped_api_tokens"
down_revision: str | None = "0017_login_ip_blocking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing HA installations keep camera controls, but lose administration.
    op.add_column(
        "api_tokens",
        sa.Column("scope", sa.String(32), nullable=False, server_default="camera_control"),
    )
    # All future inserts, including clients omitting scope, default to read-only.
    with op.batch_alter_table("api_tokens") as batch:
        batch.alter_column("scope", server_default="read_only")
        batch.create_check_constraint("api_token_scope", "scope IN ('read_only', 'camera_control')")


def downgrade() -> None:
    with op.batch_alter_table("api_tokens") as batch:
        batch.drop_constraint("api_token_scope", type_="check")
        batch.drop_column("scope")
