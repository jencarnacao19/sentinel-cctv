"""The administrator's arrangement of the camera wall.

Which cameras the wall shows, in what order, and its default grid. It applies
to every wall: the Home Assistant panel, the viewer page and the
administration's Live page. Hiding a camera only takes it off the wall; it
keeps recording and stays in playback.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AdminPrincipal
from app.database.session import get_session
from app.system.settings_store import get_setting, set_setting
from app.viewer.auth import CurrentViewer

WALL_SETTING = "wall.layout"
MAX_CAMERAS = 500


class WallLayout(BaseModel):
    columns: Literal["auto", "1", "2x2", "3x3", "4x4"] = "auto"
    #: Camera ids in wall order. Cameras missing here follow, in their usual order.
    order: list[str] = Field(default_factory=list, max_length=MAX_CAMERAS)
    hidden: list[str] = Field(default_factory=list, max_length=MAX_CAMERAS)


router = APIRouter(tags=["wall"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _load(session: AsyncSession) -> WallLayout:
    try:
        return WallLayout.model_validate(await get_setting(session, WALL_SETTING, {}))
    except ValidationError:
        # A hand-edited row must not take every wall down.
        return WallLayout()


@router.get("/viewer/wall", response_model=WallLayout, summary="Camera wall arrangement")
async def read_wall(_: CurrentViewer, session: SessionDep, response: Response) -> WallLayout:
    response.headers["Cache-Control"] = "private, no-store"
    return await _load(session)


@router.put("/wall", response_model=WallLayout, summary="Arrange the camera wall")
async def save_wall(payload: WallLayout, _: AdminPrincipal, session: SessionDep) -> WallLayout:
    await set_setting(session, WALL_SETTING, payload.model_dump())
    await session.commit()
    return payload
