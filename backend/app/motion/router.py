"""Motion event and zone endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Self

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import AdminPrincipal, CurrentPrincipal
from app.cameras import service as camera_service
from app.config import Settings, get_settings
from app.database.models import Camera, MotionEvent
from app.database.session import get_session
from app.logging_config import get_logger
from app.motion import cleanup, service
from app.motion.manager import get_manager
from app.motion.zones import (
    MAX_DETECT_ZONES,
    MAX_NAME_LENGTH,
    MAX_POINTS,
    MAX_ZONES,
    MIN_POINTS,
    ZONE_ID_PATTERN,
    MotionZone,
    ZoneKind,
    new_zone_id,
    parse_zones,
    serialise_zones,
)
from app.security.paths import UnsafePathError, resolve_within
from app.system import privacy

router = APIRouter(prefix="/events", tags=["events"])
logger = get_logger("MOTION")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

MAX_PAGE_SIZE = 500


class EventZone(BaseModel):
    id: str
    name: str


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    camera_id: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float | None
    peak_score: float
    pre_roll_start: datetime
    post_roll_end: datetime | None
    protected: bool
    has_snapshot: bool
    thumbnail_url: str | None
    snapshot_url: str | None
    playback_url: str
    """Relative link the interface and Home Assistant use to open the clip."""
    zones: list[EventZone] = Field(default_factory=list)
    """Named detection zones that saw this movement, as they were named then."""

    @classmethod
    def from_event(cls, event: MotionEvent) -> EventResponse:
        return cls(
            id=event.id,
            camera_id=event.camera_id,
            started_at=event.started_at,
            ended_at=event.ended_at,
            duration_seconds=event.duration_seconds,
            peak_score=event.peak_score,
            pre_roll_start=event.pre_roll_start,
            post_roll_end=event.post_roll_end,
            protected=event.protected,
            has_snapshot=bool(event.snapshot_path),
            # Paths on disk are never exposed; these are opaque API routes.
            thumbnail_url=(
                f"/api/v1/events/{event.id}/thumbnail" if event.thumbnail_path else None
            ),
            snapshot_url=(f"/api/v1/events/{event.id}/snapshot" if event.snapshot_path else None),
            playback_url=f"/event/{event.id}",
            zones=[EventZone(**zone) for zone in service.event_zones(event)],
        )


class EventListResponse(BaseModel):
    events: list[EventResponse]
    total: int
    limit: int
    offset: int


class EventUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protected: bool | None = None


class EventCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    min_duration_seconds: float | None = Field(default=None, ge=0)
    max_duration_seconds: float | None = Field(default=None, ge=0)
    include_protected: bool = False
    confirmation: str | None = None

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("The start must be earlier than the end.")
        if (
            self.min_duration_seconds is not None
            and self.max_duration_seconds is not None
            and self.min_duration_seconds > self.max_duration_seconds
        ):
            raise ValueError("The minimum duration cannot exceed the maximum duration.")
        return self

    def selection(self, *, freeze_new_events: bool = False) -> cleanup.EventCleanupSelection:
        return cleanup.EventCleanupSelection(
            camera_id=self.camera_id,
            start=self.start,
            end=self.end or (datetime.now(UTC) if freeze_new_events else None),
            min_duration_seconds=self.min_duration_seconds,
            max_duration_seconds=self.max_duration_seconds,
            include_protected=self.include_protected,
        )

    @property
    def is_entire_history(self) -> bool:
        return (
            self.camera_id is None
            and self.start is None
            and self.end is None
            and self.min_duration_seconds is None
            and self.max_duration_seconds is None
            and self.include_protected
        )


class EventCleanupPreviewResponse(BaseModel):
    event_count: int
    protected_count: int
    evidence_files: int
    evidence_bytes: int
    active_events_excluded: int
    required_confirmation: str


class EventCleanupStatusResponse(BaseModel):
    phase: cleanup.CleanupPhase
    job_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    camera_id: str | None
    start: datetime | None
    end: datetime | None
    min_duration_seconds: float | None
    max_duration_seconds: float | None
    include_protected: bool
    matched_events: int
    matched_evidence_files: int
    matched_evidence_bytes: int
    deleted_events: int
    deleted_evidence_files: int
    freed_bytes: int
    failed_evidence_files: int
    message: str | None
    recordings_preserved: bool
    saved_exports_preserved: bool


class MotionZoneModel(BaseModel):
    """One named zone, in normalised 0-1 coordinates.

    Normalised so a zone drawn on a 2K preview keeps working if the analysis
    resolution or the camera changes. A new zone is sent without an ``id``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, pattern=ZONE_ID_PATTERN.pattern)
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    kind: ZoneKind
    enabled: bool = True
    sensitivity: int | None = Field(default=None, ge=0, le=100)
    """Detection zones only; ``None`` follows the camera's sensitivity."""
    points: list[tuple[float, float]]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.name.strip():
            raise ValueError("Every zone needs a name.")
        if not MIN_POINTS <= len(self.points) <= MAX_POINTS:
            raise ValueError(f"Each zone needs between {MIN_POINTS} and {MAX_POINTS} points.")
        for x, y in self.points:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError("Zone coordinates must be between 0 and 1.")
        if self.kind is ZoneKind.IGNORE and self.sensitivity is not None:
            raise ValueError("Only detection zones have their own sensitivity.")
        return self

    @classmethod
    def from_zone(cls, zone: MotionZone) -> MotionZoneModel:
        return cls(
            id=zone.id,
            name=zone.name,
            kind=zone.kind,
            enabled=zone.enabled,
            sensitivity=zone.sensitivity,
            points=list(zone.points),
        )

    def to_zone(self) -> MotionZone:
        return MotionZone(
            id=self.id or new_zone_id(),
            name=self.name.strip(),
            kind=self.kind,
            points=tuple((round(x, 4), round(y, 4)) for x, y in self.points),
            enabled=self.enabled,
            sensitivity=self.sensitivity,
        )


class MotionZonesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zones: list[MotionZoneModel] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if len(self.zones) > MAX_ZONES:
            raise ValueError(f"At most {MAX_ZONES} zones per camera.")
        if sum(zone.kind is ZoneKind.DETECT for zone in self.zones) > MAX_DETECT_ZONES:
            raise ValueError(f"At most {MAX_DETECT_ZONES} detection zones per camera.")
        names = [zone.name.strip().casefold() for zone in self.zones]
        if len(set(names)) != len(names):
            # Each name becomes a Home Assistant entity; two alike are unusable.
            raise ValueError("Zone names must be different from each other.")
        ids = [zone.id for zone in self.zones if zone.id]
        if len(set(ids)) != len(ids):
            raise ValueError("Zone ids must be different from each other.")
        return self


class MotionZonesResponse(BaseModel):
    camera_id: str
    zones: list[MotionZoneModel]


async def _load_event(session: AsyncSession, event_id: str) -> MotionEvent:
    event = await session.get(MotionEvent, event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")
    return event


@router.get("", response_model=EventListResponse, summary="List motion events")
async def list_events(
    _: CurrentPrincipal,
    session: SessionDep,
    camera_id: str | None = None,
    start: datetime | None = Query(default=None, description="Inclusive UTC lower bound"),
    end: datetime | None = Query(default=None, description="Exclusive UTC upper bound"),
    protected: bool | None = None,
    min_duration_seconds: float | None = Query(default=None, ge=0),
    max_duration_seconds: float | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> EventListResponse:
    filters = []
    if camera_id:
        filters.append(MotionEvent.camera_id == camera_id)
    if start is not None:
        # An event that began before the window but is still running overlaps it.
        filters.append((MotionEvent.ended_at.is_(None)) | (MotionEvent.ended_at > start))
    if end is not None:
        filters.append(MotionEvent.started_at < end)
    if protected is not None:
        filters.append(MotionEvent.protected.is_(protected))
    if min_duration_seconds is not None:
        filters.append(MotionEvent.duration_seconds >= min_duration_seconds)
    if max_duration_seconds is not None:
        filters.append(MotionEvent.duration_seconds <= max_duration_seconds)

    total = await session.scalar(select(func.count()).select_from(MotionEvent).where(*filters))
    rows = await session.execute(
        select(MotionEvent)
        .where(*filters)
        .order_by(MotionEvent.started_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return EventListResponse(
        events=[EventResponse.from_event(row) for row in rows.scalars()],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.get("/status", summary="Motion worker status")
async def motion_status(_: CurrentPrincipal) -> dict[str, object]:
    return {"workers": get_manager().worker_status()}


def _cleanup_confirmation(payload: EventCleanupRequest) -> str:
    return "DELETE ALL EVENTS" if payload.is_entire_history else "DELETE SELECTED EVENTS"


@router.get("/summary", summary="Motion event library summary")
async def event_summary(_: CurrentPrincipal, session: SessionDep) -> dict[str, object]:
    totals = (
        await session.execute(
            select(
                func.count(MotionEvent.id),
                func.coalesce(func.sum(case((MotionEvent.protected.is_(True), 1), else_=0)), 0),
                func.coalesce(
                    func.sum(case((MotionEvent.snapshot_path.is_not(None), 1), else_=0)), 0
                ),
                func.min(MotionEvent.started_at),
                func.max(MotionEvent.started_at),
            )
        )
    ).one()
    rows = await session.execute(
        select(
            MotionEvent.camera_id,
            Camera.name,
            func.count(MotionEvent.id),
            func.min(MotionEvent.started_at),
            func.max(MotionEvent.started_at),
        )
        .join(Camera, Camera.id == MotionEvent.camera_id)
        .group_by(MotionEvent.camera_id, Camera.name)
        .order_by(func.count(MotionEvent.id).desc())
    )
    return {
        "total": int(totals[0] or 0),
        "protected": int(totals[1] or 0),
        "with_snapshot": int(totals[2] or 0),
        "earliest": totals[3],
        "latest": totals[4],
        "cameras": [
            {
                "camera_id": camera_id,
                "camera_name": camera_name,
                "count": int(count),
                "earliest": earliest,
                "latest": latest,
            }
            for camera_id, camera_name, count, earliest, latest in rows.all()
        ],
    }


@router.post(
    "/cleanup/preview",
    response_model=EventCleanupPreviewResponse,
    summary="Preview explicit motion-event cleanup",
)
async def preview_event_cleanup(
    payload: EventCleanupRequest,
    _: AdminPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> EventCleanupPreviewResponse:
    if payload.camera_id is not None and await session.get(Camera, payload.camera_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found.")
    result = await cleanup.preview(session, payload.selection(), settings)
    return EventCleanupPreviewResponse(
        event_count=result.event_count,
        protected_count=result.protected_count,
        evidence_files=result.evidence_files,
        evidence_bytes=result.evidence_bytes,
        active_events_excluded=result.active_events_excluded,
        required_confirmation=_cleanup_confirmation(payload),
    )


@router.get(
    "/cleanup",
    response_model=EventCleanupStatusResponse,
    summary="Motion-event cleanup progress",
)
async def event_cleanup_status(_: AdminPrincipal) -> EventCleanupStatusResponse:
    return EventCleanupStatusResponse.model_validate(cleanup.get_manager().snapshot().as_dict())


@router.post(
    "/cleanup",
    response_model=EventCleanupStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start explicit motion-event cleanup",
)
async def start_event_cleanup(
    payload: EventCleanupRequest,
    _: AdminPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> EventCleanupStatusResponse:
    required = _cleanup_confirmation(payload)
    if payload.confirmation != required:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f'Type "{required}" to confirm this irreversible cleanup.',
        )
    if payload.camera_id is not None and await session.get(Camera, payload.camera_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found.")

    # Freeze the upper bound so events detected while 39,000 older rows are
    # removed remain visible and the job always reaches a finite end.
    selection = payload.selection(freeze_new_events=True)
    expected = await cleanup.preview(session, selection, settings)
    if expected.event_count == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No completed motion events match this selection.",
        )
    try:
        snapshot = await cleanup.get_manager().start(selection, settings, expected)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return EventCleanupStatusResponse.model_validate(snapshot.as_dict())


@router.delete(
    "/cleanup",
    response_model=EventCleanupStatusResponse,
    summary="Stop a running motion-event cleanup",
)
async def cancel_event_cleanup(_: AdminPrincipal) -> EventCleanupStatusResponse:
    return EventCleanupStatusResponse.model_validate(
        (await cleanup.get_manager().cancel()).as_dict()
    )


@router.get("/{event_id}", response_model=EventResponse, summary="Get one event")
async def get_event(event_id: str, _: CurrentPrincipal, session: SessionDep) -> EventResponse:
    return EventResponse.from_event(await _load_event(session, event_id))


@router.patch("/{event_id}", response_model=EventResponse, summary="Protect an event")
async def update_event(
    event_id: str, payload: EventUpdate, _: AdminPrincipal, session: SessionDep
) -> EventResponse:
    event = await _load_event(session, event_id)
    if payload.protected is not None:
        event.protected = payload.protected
    await session.commit()
    return EventResponse.from_event(event)


async def _serve_image(
    session: AsyncSession, settings: Settings, event_id: str, *, thumbnail: bool
) -> FileResponse:
    event = await _load_event(session, event_id)
    relative = event.thumbnail_path if thumbnail else event.snapshot_path
    root = settings.thumbnails_dir if thumbnail else settings.snapshots_dir

    if not relative:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="This event has no image."
        )
    try:
        absolute = resolve_within(root / relative, root)
    except (UnsafePathError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Image not found."
        ) from exc
    if not absolute.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found.")

    return FileResponse(
        absolute,
        media_type="image/jpeg",
        # Immutable: an event's image never changes once written, so the browser
        # can keep it for a timeline that scrolls back over the same events.
        headers={"Cache-Control": "private, max-age=86400, immutable"},
    )


@router.get(
    "/{event_id}/thumbnail",
    summary="Event thumbnail",
    response_class=FileResponse,
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def event_thumbnail(
    event_id: str, _: CurrentPrincipal, session: SessionDep, settings: SettingsDep
) -> FileResponse:
    return await _serve_image(session, settings, event_id, thumbnail=True)


@router.get(
    "/{event_id}/snapshot",
    summary="Event snapshot",
    response_class=FileResponse,
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def event_snapshot(
    event_id: str, _: CurrentPrincipal, session: SessionDep, settings: SettingsDep
) -> FileResponse:
    return await _serve_image(session, settings, event_id, thumbnail=False)


# --- Motion zones -------------------------------------------------------

zones_router = APIRouter(prefix="/cameras", tags=["motion"])


@zones_router.get(
    "/{camera_id}/motion-zones",
    response_model=MotionZonesResponse,
    summary="Named detection and ignore zones",
)
async def get_motion_zones(
    camera_id: str, _: CurrentPrincipal, session: SessionDep
) -> MotionZonesResponse:
    camera = await session.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found.")
    return MotionZonesResponse(
        camera_id=camera.id,
        zones=[MotionZoneModel.from_zone(zone) for zone in parse_zones(camera.motion_zones)],
    )


@zones_router.put(
    "/{camera_id}/motion-zones",
    response_model=MotionZonesResponse,
    summary="Replace the motion zones",
)
async def set_motion_zones(
    camera_id: str, payload: MotionZonesUpdate, _: AdminPrincipal, session: SessionDep
) -> MotionZonesResponse:
    camera = await session.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found.")

    zones = [zone.to_zone() for zone in payload.zones]
    camera.motion_zones = serialise_zones(zones)
    await session.commit()
    logger.info(
        "Motion zones updated",
        extra={"camera": camera.name, "camera_id": camera.id, "zones": len(zones)},
    )

    # The worker holds compiled masks, so it has to be rebuilt with the camera.
    await _restart_worker(camera_id)
    return MotionZonesResponse(
        camera_id=camera.id, zones=[MotionZoneModel.from_zone(zone) for zone in zones]
    )


@zones_router.get(
    "/{camera_id}/motion-preview",
    summary="A frame to draw motion zones on",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def motion_preview(camera_id: str, _: AdminPrincipal, session: SessionDep) -> Response:
    """The current view, for the zone editor to draw over."""
    privacy.require_camera_access()
    from app.cameras.snapshot import SnapshotError, capture_snapshot

    try:
        camera = await camera_service.get_camera(session, camera_id)
    except camera_service.CameraNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found."
        ) from exc

    url = camera_service.resolve_stream_url(camera, sub=bool(camera.sub_stream_url))
    if url is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This camera has no stream URL."
        )
    try:
        image = await capture_snapshot(url, width=1280)
    except SnapshotError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return Response(content=image, media_type="image/jpeg")


async def _restart_worker(camera_id: str) -> None:
    """Failures are logged and swallowed: the zones are already saved, and the
    worker picks them up on its next restart regardless."""
    try:
        await get_manager().restart_camera(camera_id)
    except Exception:
        logger.warning("Could not restart the motion worker", exc_info=True)
