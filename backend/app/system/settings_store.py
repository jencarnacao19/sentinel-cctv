"""Typed access to the ``app_settings`` key/value table.

Runtime configuration the user edits in the browser lives here rather than in a
file, so nothing needs a container restart and there is no YAML to hand-edit.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import utcnow
from app.database.models import AppSetting

T = TypeVar("T")

# --- Known keys ---------------------------------------------------------
SETUP_COMPLETED = "setup.completed"
STORAGE_PATH = "storage.path"
STORAGE_RETENTION_DAYS = "storage.retention_days"
STORAGE_MAX_BYTES = "storage.max_bytes"
STORAGE_MIN_FREE_BYTES = "storage.min_free_bytes"
TIMEZONE = "system.timezone"
SEGMENT_SECONDS = "recording.segment_seconds"
RECORDER_DIRECT_CAMERA_IDS = "recording.direct_camera_ids"
"""Superseded by :data:`RECORDER_SOURCE_POLICY`; read once at start-up so an
upgrade can log that it was discarded, never written again."""
RECORDER_SOURCE_POLICY = "recording.source_policy"
MOTION_PRE_ROLL_SECONDS = "motion.pre_roll_seconds"
MOTION_POST_ROLL_SECONDS = "motion.post_roll_seconds"
MOTION_END_GRACE_SECONDS = "motion.end_grace_seconds"
PRIVACY_MODE = "privacy.mode"
LOGIN_IP_BLOCK = "security.login_ip_block"

DEFAULTS: dict[str, Any] = {
    SETUP_COMPLETED: False,
    STORAGE_PATH: None,
    STORAGE_RETENTION_DAYS: 14,
    STORAGE_MAX_BYTES: None,
    # The Proxmox installer defaults to a 16 GiB root disk. A 100 GiB reserve
    # would therefore make every recording eligible for deletion forever.
    STORAGE_MIN_FREE_BYTES: 5 * 1024**3,
    TIMEZONE: "UTC",
    SEGMENT_SECONDS: 60,
    RECORDER_DIRECT_CAMERA_IDS: [],
    RECORDER_SOURCE_POLICY: {},
    MOTION_PRE_ROLL_SECONDS: 5,
    MOTION_POST_ROLL_SECONDS: 10,
    MOTION_END_GRACE_SECONDS: 8,
    PRIVACY_MODE: {"enabled": False},
    LOGIN_IP_BLOCK: {
        "enabled": False,
        "failure_threshold": 1,
        "block_duration_seconds": None,
    },
}


async def get_setting(session: AsyncSession, key: str, default: Any = ...) -> Any:
    row = await session.get(AppSetting, key)
    if row is None:
        if default is not ...:
            return default
        return DEFAULTS.get(key)
    try:
        return json.loads(row.value)
    except json.JSONDecodeError:
        # A hand-edited or corrupted row must not take the whole app down.
        return DEFAULTS.get(key) if default is ... else default


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
    encoded = json.dumps(value)
    updated_at = utcnow()
    insert_statement = sqlite_insert(AppSetting).values(
        key=key,
        value=encoded,
        updated_at=updated_at,
    )
    upsert_statement = insert_statement.on_conflict_do_update(
        index_elements=["key"],
        set_={"value": encoded, "updated_at": updated_at},
    ).returning(AppSetting)
    # ``populate_existing`` keeps an AppSetting already present in this
    # session's identity map in step with the atomic database upsert. Without
    # it, a get immediately after a set in the same request could see the old
    # JSON even though SQLite already holds the new value.
    await session.scalar(upsert_statement, execution_options={"populate_existing": True})
    await session.flush()


async def get_many(session: AsyncSession, keys: list[str]) -> dict[str, Any]:
    result = await session.execute(select(AppSetting).where(AppSetting.key.in_(keys)))
    stored = {row.key: row.value for row in result.scalars()}
    values: dict[str, Any] = {}
    for key in keys:
        raw = stored.get(key)
        if raw is None:
            values[key] = DEFAULTS.get(key)
            continue
        try:
            values[key] = json.loads(raw)
        except json.JSONDecodeError:
            values[key] = DEFAULTS.get(key)
    return values


async def set_many(session: AsyncSession, values: dict[str, Any]) -> None:
    for key, value in values.items():
        await set_setting(session, key, value)


async def is_setup_completed(session: AsyncSession) -> bool:
    return bool(await get_setting(session, SETUP_COMPLETED, False))
