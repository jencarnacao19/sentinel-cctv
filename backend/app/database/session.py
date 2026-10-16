"""Async engine and session factory.

SQLite is the default because it needs no administration -- a home user should
never be asked to install PostgreSQL. WAL journalling lets the recorder index
segments while the API serves the timeline, which is the concurrency pattern
Sentinel actually has. The engine URL is the only thing that would change to
move to PostgreSQL later.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings

logger = logging.getLogger("sentinel.database")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Per-connection SQLite tuning.

    ``busy_timeout`` matters most: without it a writer holding the lock during a
    retention sweep would surface as "database is locked" to the API.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA temp_store=MEMORY")
    finally:
        cursor.close()


def create_engine_from_settings(settings: Settings) -> AsyncEngine:
    is_sqlite = settings.database_url.startswith("sqlite")
    if is_sqlite:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(
        settings.database_url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        # SQLite's driver serialises writes anyway; a small pool avoids piling up
        # connections that would all contend for the same file lock.
        **({"connect_args": {"timeout": 30}} if is_sqlite else {}),
    )
    if is_sqlite:
        event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        _engine = create_engine_from_settings(settings)
        _session_factory = async_sessionmaker(
            _engine,
            expire_on_commit=False,
            autoflush=False,
        )
        logger.debug("Database engine created")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None  # noqa: S101 - established by get_engine
    return _session_factory


async def dispose_engine() -> None:
    """Close pooled connections. Called on shutdown and between tests."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def set_engine(engine: AsyncEngine | None) -> None:
    """Test helper: inject a pre-built engine."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = (
        async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        if engine is not None
        else None
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that rolls back on error."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Context manager for background workers: commits on success."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_database() -> bool:
    """Lightweight connectivity probe used by the health endpoint."""
    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        logger.warning("Database health check failed", exc_info=True)
        return False
    return True
