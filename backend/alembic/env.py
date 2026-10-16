"""Alembic environment.

Migrations run synchronously against the same SQLite file the async application
uses. ``render_as_batch`` is essential: SQLite cannot ALTER most columns, and
batch mode makes Alembic rebuild the table instead of failing.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import models  # noqa: E402,F401  (registers the tables)
from app.database.base import Base, UTCDateTime  # noqa: E402

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

if not config.attributes.get("url_set_programmatically"):
    # Running `alembic` by hand: take the database location from the same
    # settings the application uses, so SENTINEL_DATA_DIR works here too and the
    # URL in alembic.ini stays a placeholder nobody has to edit.
    from app.config import get_settings

    settings = get_settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    config.set_main_option("sqlalchemy.url", settings.sync_database_url.replace("%", "%%"))

target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render UTCDateTime with an import Alembic can actually resolve.

    Without this, autogenerate emits a bare ``app.database.base.UTCDateTime()``
    reference in a file that never imports it, and the migration fails to run.
    """
    if type_ == "type" and isinstance(obj, UTCDateTime):
        assert hasattr(autogen_context, "imports")  # noqa: S101 - Alembic contract
        autogen_context.imports.add("from app.database.base import UTCDateTime")  # type: ignore[attr-defined]
        return "UTCDateTime()"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
