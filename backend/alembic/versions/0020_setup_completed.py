"""Mark installations that already have an account as set up.

The first-run wizard now continues past the administrator account, and an
administrator whose setup is not finished is sent back into it. Installations
created before the wizard existed were set up by hand; sending them through it
again after an upgrade would be wrong.

Revision ID: 0020_setup_completed
Revises: 0019_motion_event_zones
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_setup_completed"
down_revision: str | None = "0019_motion_event_zones"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite needs a WHERE on the SELECT to parse the ON CONFLICT clause; the
    # EXISTS is that WHERE, and on a fresh database it inserts nothing.
    op.execute(
        sa.text(
            "INSERT INTO app_settings (key, value, updated_at) "
            "SELECT 'setup.completed', 'true', CURRENT_TIMESTAMP "
            "WHERE EXISTS (SELECT 1 FROM users) "
            "ON CONFLICT (key) DO UPDATE SET value = 'true', updated_at = CURRENT_TIMESTAMP"
        )
    )


def downgrade() -> None:
    # Earlier revisions never read this setting, so leaving it is harmless, and
    # removing it would put a set-up installation back into the wizard after a
    # later upgrade.
    pass
