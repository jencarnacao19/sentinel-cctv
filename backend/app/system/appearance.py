"""Account-scoped presentation preferences; never change camera behaviour."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AdminPrincipal, CurrentUser, OptionalPrincipal
from app.database.session import get_session
from app.system import settings_store
from app.viewer.auth import CurrentViewer

router = APIRouter(tags=["appearance"])
Session = Annotated[AsyncSession, Depends(get_session)]
POLICY_KEY = "appearance.sound_policy"


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["system", "light", "dark"] = "system"
    palette: Literal["slate", "ocean", "forest", "sand"] = "slate"
    hover_sound: StrictBool = True


class PreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A stale browser save must not follow a logout into another user's account.
    profile_id: str
    preferences: Preferences


class SoundPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hover_sounds_allowed: StrictBool = True


class AppearanceResponse(SoundPolicy):
    profile_id: str | None = None
    preferences: Preferences | None = None


@router.get("/viewer/appearance", response_model=AppearanceResponse)
async def read_appearance(
    _: CurrentViewer, principal: OptionalPrincipal, session: Session, response: Response
) -> AppearanceResponse:
    response.headers["Cache-Control"] = "private, no-store"
    policy = await settings_store.get_setting(session, POLICY_KEY, SoundPolicy().model_dump())
    profile_id = principal.id if principal is not None and principal.kind == "user" else None
    preferences = None
    if profile_id is not None:
        stored = await settings_store.get_setting(
            session, f"appearance.user.{profile_id}", Preferences().model_dump()
        )
        preferences = Preferences.model_validate(stored)
    return AppearanceResponse(**policy, profile_id=profile_id, preferences=preferences)


@router.put("/appearance/me", response_model=Preferences)
async def save_appearance(
    payload: PreferenceUpdate, user: CurrentUser, session: Session
) -> Preferences:
    if payload.profile_id != user.id:
        raise HTTPException(
            status_code=409, detail="The signed-in account changed. Reload this page."
        )
    await settings_store.set_setting(
        session, f"appearance.user.{user.id}", payload.preferences.model_dump()
    )
    await session.commit()
    return payload.preferences


@router.put("/appearance/sound-policy", response_model=SoundPolicy)
async def save_sound_policy(
    payload: SoundPolicy, _: AdminPrincipal, session: Session
) -> SoundPolicy:
    await settings_store.set_setting(session, POLICY_KEY, payload.model_dump())
    await session.commit()
    return payload
