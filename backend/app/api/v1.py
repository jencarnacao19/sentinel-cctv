"""Versioned API router.

Every endpoint lives under ``/api/v1``. Feature routers are attached here as
each phase lands, so the mounted surface always matches what is implemented --
there are no placeholder routes.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.auth.router import router as auth_router
from app.cameras.router import router as cameras_router
from app.cameras.tapo_router import router as tapo_router
from app.discovery.router import router as discovery_router
from app.integrations.panel import router as ha_panel_router
from app.integrations.router import router as integrations_router
from app.motion.router import router as events_router
from app.motion.router import zones_router as motion_zones_router
from app.realtime.router import router as realtime_router
from app.recording.router import router as recordings_router
from app.recording.timeline_router import exports_router
from app.recording.timeline_router import router as timeline_router
from app.reliability.router import router as reliability_router
from app.storage.router import router as storage_router
from app.streaming.router import router as streams_router
from app.system.appearance import router as appearance_router
from app.system.router import router as system_router
from app.viewer.router import router as viewer_router

API_V1_PREFIX = "/api/v1"

api_router = APIRouter(prefix=API_V1_PREFIX)
api_router.include_router(system_router)
api_router.include_router(appearance_router)
api_router.include_router(auth_router)
api_router.include_router(viewer_router)
api_router.include_router(cameras_router)
# Mounted after the camera router so /cameras/{id}/motion-zones resolves; both
# share the /cameras prefix but have no overlapping paths.
api_router.include_router(motion_zones_router)
# Also under /cameras. Vendor-specific local control, kept out of the generic
# camera router so ONVIF stays the first-class path and Tapo the fallback.
api_router.include_router(tapo_router)
# Discovery ends at the same create_camera call the manual form uses, so it is
# mounted alongside the camera routes rather than owning a surface of its own.
api_router.include_router(discovery_router)
# Also under /cameras, for the per-camera timeline window.
api_router.include_router(timeline_router)
api_router.include_router(streams_router)
api_router.include_router(recordings_router)
api_router.include_router(exports_router)
api_router.include_router(storage_router)
# Reads only telemetry the recording/motion/streaming managers already hold, so
# it is mounted alongside them rather than owning any camera connection.
api_router.include_router(reliability_router)
api_router.include_router(events_router)
# The WebSocket shares the /events prefix with the REST event list: one is
# GET /events, the other WS /events/stream, so they never collide.
api_router.include_router(realtime_router)
api_router.include_router(integrations_router)
api_router.include_router(ha_panel_router)
