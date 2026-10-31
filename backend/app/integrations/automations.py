"""Home Assistant automations arranged in Sentinel's administration.

The administrator saves recipes here; the Home Assistant integration, once
its owner allows it there, turns each into a real Home Assistant automation.
Sentinel only stores what to build: it never reaches into Home Assistant, and
the integration checks every automation against its own list of allowed
actions before writing it.

The integration reports back the targets the recipes can use (phones, lights,
people, alarm panels) and, per recipe, what it did.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AdminPrincipal, IntegrationReadPrincipal
from app.database.session import get_session
from app.events.bus import AUTOMATIONS_CHANGED, publish
from app.system.settings_store import get_setting, set_setting

RECIPES_SETTING = "ha.automations"
TARGETS_SETTING = "ha.targets"
STATUS_SETTING = "ha.automations_status"
MAX_RECIPES = 100

EntityList = Annotated[list[str], Field(max_length=50)]


class Recipe(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]{8,32}$")
    kind: Literal["motion_alert", "system_alert", "motion_actions", "presence_recording"]
    name: str = Field(min_length=1, max_length=100)
    camera_id: str | None = Field(default=None, max_length=64)
    zone_id: str | None = Field(default=None, max_length=64)
    camera_ids: Annotated[list[Annotated[str, Field(max_length=64)]], Field(max_length=50)] = []
    notify: Annotated[
        list[Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]], Field(max_length=20)
    ] = []
    lights: Annotated[
        list[Annotated[str, Field(pattern=r"^light\.[a-z0-9_]+$")]], Field(max_length=20)
    ] = []
    when: Literal["always", "night", "away", "alarm_armed"] = "always"
    alarm_entity: str | None = Field(default=None, pattern=r"^alarm_control_panel\.[a-z0-9_]+$")
    image: bool = True
    cooldown_minutes: int = Field(default=5, ge=0, le=240)
    minutes: int = Field(default=5, ge=1, le=240)
    trigger_alarm: bool = False
    storage_below_gb: int = Field(default=20, ge=1, le=100_000)
    message: str = Field(default="", max_length=300)
    #: Raised by "Give back to Sentinel": overwrite even if it was edited in Home Assistant.
    reclaim: int = Field(default=0, ge=0)


class Target(BaseModel):
    id: str = Field(max_length=255)
    name: str = Field(max_length=255)


class Targets(BaseModel):
    #: The integration option that lets Sentinel create automations.
    allowed: bool = False
    #: Whether Home Assistant keeps its editor's automations in automations.yaml.
    automations_file: bool = False
    notify: Annotated[list[Target], Field(max_length=200)] = []
    lights: Annotated[list[Target], Field(max_length=500)] = []
    people: Annotated[list[Target], Field(max_length=100)] = []
    alarms: Annotated[list[Target], Field(max_length=20)] = []
    reported_at: str | None = None


class RecipeStatus(BaseModel):
    state: Literal["created", "edited_in_ha", "refused", "waiting_permission", "unsupported"]
    message: str = Field(default="", max_length=500)


class AutomationsState(BaseModel):
    recipes: list[Recipe]
    targets: Targets | None
    status: dict[str, RecipeStatus]


class RecipesUpdate(BaseModel):
    recipes: Annotated[list[Recipe], Field(max_length=MAX_RECIPES)]


class StatusUpdate(BaseModel):
    status: Annotated[dict[str, RecipeStatus], Field(max_length=MAX_RECIPES)]


router = APIRouter(prefix="/integrations/home-assistant", tags=["home-assistant"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _machine(principal: IntegrationReadPrincipal) -> None:
    if not principal.is_machine:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the Home Assistant integration reports this.",
        )


async def _state(session: AsyncSession) -> AutomationsState:
    recipes = await get_setting(session, RECIPES_SETTING, None) or []
    targets = await get_setting(session, TARGETS_SETTING, None)
    status_map = await get_setting(session, STATUS_SETTING, None) or {}
    return AutomationsState(
        recipes=[Recipe.model_validate(item) for item in recipes],
        targets=Targets.model_validate(targets) if targets else None,
        status={key: RecipeStatus.model_validate(value) for key, value in status_map.items()},
    )


@router.get("/automations", response_model=AutomationsState, summary="Automation recipes")
async def read_automations(_: IntegrationReadPrincipal, session: SessionDep) -> AutomationsState:
    return await _state(session)


@router.put("/automations", response_model=AutomationsState, summary="Save automation recipes")
async def save_automations(
    payload: RecipesUpdate, _: AdminPrincipal, session: SessionDep
) -> AutomationsState:
    ids = [recipe.id for recipe in payload.recipes]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=422, detail="Each recipe needs its own id.")
    await set_setting(session, RECIPES_SETTING, [recipe.model_dump() for recipe in payload.recipes])
    await session.commit()
    publish(AUTOMATIONS_CHANGED, {"count": len(ids)})
    return await _state(session)


@router.put("/targets", status_code=204, summary="Home Assistant targets for recipes")
async def report_targets(
    payload: Targets, principal: IntegrationReadPrincipal, session: SessionDep
) -> None:
    _machine(principal)
    await set_setting(session, TARGETS_SETTING, payload.model_dump())
    await session.commit()


@router.put("/automations/status", status_code=204, summary="What the integration did")
async def report_status(
    payload: StatusUpdate, principal: IntegrationReadPrincipal, session: SessionDep
) -> None:
    _machine(principal)
    await set_setting(
        session, STATUS_SETTING, {key: value.model_dump() for key, value in payload.status.items()}
    )
    await session.commit()
