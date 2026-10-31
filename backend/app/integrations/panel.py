"""The Home Assistant panel link, created from the administration.

The link carries a viewer key that the administrator creates here instead of
writing ``SENTINEL_VIEWER_KEY`` into the deployment ``.env``; that variable
keeps working. Only a SHA-256 hash of the key is stored, as for API tokens, and
creating a new link replaces it, which signs out every session of the old one.

The hash and the Home Assistant origins are also cached in this module: the
viewer-key checks and the Content-Security-Policy header run outside any
database session, on every request.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AdminPrincipal
from app.config import get_settings, validate_frame_ancestor
from app.database.session import get_session
from app.system.settings_store import get_setting, set_setting

KEY_HASH_SETTING = "ha_panel.key_hash"
ORIGINS_SETTING = "ha_panel.origins"
MAX_ORIGINS = 10

_key_hash: str | None = None
_origins: tuple[str, ...] = ()


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


async def load_panel_state(session: AsyncSession) -> None:
    """Read the stored link and origins into the cache. Called at start-up."""
    global _key_hash, _origins
    stored = await get_setting(session, KEY_HASH_SETTING, None)
    _key_hash = stored if isinstance(stored, str) and stored else None
    origins = await get_setting(session, ORIGINS_SETTING, [])
    _origins = tuple(o for o in origins if isinstance(o, str)) if isinstance(origins, list) else ()


def panel_key_matches(candidate: str) -> bool:
    return _key_hash is not None and secrets.compare_digest(_key_hash, _hash(candidate))


def panel_link_exists() -> bool:
    return _key_hash is not None


def panel_ancestors() -> tuple[str, ...]:
    return _origins


class PanelStatus(BaseModel):
    link_exists: bool
    #: A key from SENTINEL_VIEWER_KEY also opens the panel; the page says so.
    env_key: bool
    origins: list[str]


class PanelOrigins(BaseModel):
    origins: list[str] = Field(max_length=MAX_ORIGINS)

    @field_validator("origins")
    @classmethod
    def _valid_origins(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(validate_frame_ancestor(origin.strip()) for origin in value))


class PanelLink(BaseModel):
    #: Shown once. Only its hash is kept.
    key: str


router = APIRouter(prefix="/integrations/home-assistant/panel", tags=["home-assistant"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _status() -> PanelStatus:
    return PanelStatus(
        link_exists=panel_link_exists(),
        env_key=get_settings().viewer_key is not None,
        origins=list(_origins),
    )


@router.get("", response_model=PanelStatus, summary="Home Assistant panel link state")
async def panel_status(_: AdminPrincipal) -> PanelStatus:
    return _status()


@router.put("/origins", response_model=PanelStatus, summary="Home Assistant addresses")
async def set_panel_origins(
    payload: PanelOrigins, _: AdminPrincipal, session: SessionDep
) -> PanelStatus:
    global _origins
    await set_setting(session, ORIGINS_SETTING, payload.origins)
    await session.commit()
    _origins = tuple(payload.origins)
    return _status()


@router.post("/link", response_model=PanelLink, summary="Create the Home Assistant panel link")
async def create_panel_link(_: AdminPrincipal, session: SessionDep) -> PanelLink:
    global _key_hash
    key = secrets.token_urlsafe(32)
    await set_setting(session, KEY_HASH_SETTING, _hash(key))
    await session.commit()
    _key_hash = _hash(key)
    return PanelLink(key=key)
