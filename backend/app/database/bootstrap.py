"""Schema bootstrap.

On every start Sentinel brings the database up to the latest revision. If the
file does not exist it is created and fully migrated; if it does, only the
pending revisions run. The database is never dropped or recreated to apply an
update -- an upgrade must never cost the user their recordings index.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic.config import Config

from alembic import command
from app.config import Settings
from app.logging_config import get_logger

logger = get_logger("SYSTEM")

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_DIR = BACKEND_ROOT / "alembic"


def build_alembic_config(settings: Settings) -> Config:
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    # Escaped because ConfigParser treats a bare '%' as interpolation syntax and
    # a Windows or URL-encoded path can legitimately contain one.
    config.set_main_option("sqlalchemy.url", settings.sync_database_url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    config.attributes["url_set_programmatically"] = True
    return config


def upgrade_to_head(settings: Settings) -> None:
    """Synchronously apply pending migrations."""
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    config = build_alembic_config(settings)
    logger.info("Applying database migrations", extra={"database": str(settings.database_path)})
    command.upgrade(config, "head")
    logger.info("Database schema is up to date")


async def upgrade_to_head_async(settings: Settings) -> None:
    """Run migrations off the event loop; Alembic's driver is synchronous."""
    await asyncio.to_thread(upgrade_to_head, settings)


def current_revision(settings: Settings) -> str | None:
    """Return the revision the database is stamped with, if any."""
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    if not settings.database_path.exists():
        return None
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
