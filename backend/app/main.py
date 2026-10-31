"""FastAPI application factory and process lifespan.

Start-up order matters and is deliberate (see docs/architecture.md):
database -> storage -> go2rtc -> API -> recorders -> motion workers. Each phase
of the project attaches its own start-up hook here, so a partially configured
install still boots and shows the user what is missing instead of crashing.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api import api_router
from app.cameras import vendor_pool
from app.cameras.ptz import get_manager as get_ptz_manager
from app.config import Settings, get_settings
from app.database.bootstrap import upgrade_to_head_async
from app.database.session import check_database, dispose_engine, get_engine, session_scope
from app.integrations import mdns
from app.integrations.panel import load_panel_state, panel_ancestors
from app.logging_config import configure_logging, get_logger
from app.motion.cleanup import get_manager as get_event_cleanup_manager
from app.motion.manager import get_manager as get_motion_manager
from app.recording.export import get_manager as get_export_manager
from app.recording.manager import get_manager as get_recording_manager
from app.reliability.monitor import get_monitor as get_reliability_monitor
from app.security.origin import request_origin
from app.security.paths import UnsafePathError, resolve_within
from app.security.redaction import redact_text
from app.storage.cleanup import get_manager as get_cleanup_manager
from app.streaming.manager import get_manager as get_streaming_manager
from app.system import address, health, privacy, settings_store

logger = get_logger("SYSTEM")

BACKEND_ROOT = Path(__file__).resolve().parents[1]
#: Built frontend, copied here by the Docker image build.
FRONTEND_DIST = BACKEND_ROOT / "static"

_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_VIEWER_COOKIE_NAME = "sentinel_viewer"
_VIEWER_KEY_HEADER = "x-sentinel-viewer-key"


def _register_core_health_probes(settings: Settings) -> None:
    async def database_probe() -> health.ComponentHealth:
        ok = await check_database()
        return health.ComponentHealth(
            state=health.ComponentState.OK if ok else health.ComponentState.FAILED,
            detail=None if ok else "Database is not reachable.",
        )

    def storage_probe() -> health.ComponentHealth:
        # The configured recordings directory is validated by the storage
        # manager in phase 5. Until then only the data directory is required.
        data_dir = settings.data_dir
        if not data_dir.exists():
            return health.ComponentHealth(
                state=health.ComponentState.FAILED,
                detail=f"Data directory is missing: {data_dir}",
            )
        if not os.access(data_dir, os.W_OK):
            return health.ComponentHealth(
                state=health.ComponentState.FAILED,
                detail=f"Data directory is not writable: {data_dir}",
            )
        return health.ComponentHealth(state=health.ComponentState.OK)

    health.register("database", database_probe, critical=True)
    health.register("storage", storage_probe, critical=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)
    logger.info("Starting Sentinel CCTV", extra={"version": __version__})

    settings.ensure_directories()
    settings.load_or_create_secret()

    await upgrade_to_head_async(settings)
    get_engine()

    # Privacy is loaded before any process is allowed to contact a camera. A
    # reboot therefore cannot silently resume feeds that an administrator had
    # explicitly paused.
    async with session_scope() as session:
        await privacy.get_manager().load(session)
        await load_panel_state(session)
    await privacy.get_manager().apply_loaded_state()

    # Keep one small HTTP connection pool for ONVIF controls. A PTZ button
    # press should be one LAN request, never a fresh discovery/handshake flow.
    ptz = get_ptz_manager()
    await ptz.start()

    _register_core_health_probes(settings)

    # go2rtc comes up after the database so it can be configured from the camera
    # list, and before the recorders, which will pull their input from it.
    streaming = get_streaming_manager()
    await streaming.start()
    health.register("go2rtc", streaming.health_probe, critical=False)
    if settings.record_via_go2rtc and not settings.testing:
        # The manager itself starts non-blocking so the UI can still report a
        # failed relay. Recorders, however, must not all hit a listener that is
        # still opening and simultaneously fall back to direct camera clients.
        await streaming.wait_until_ready()

    # Recorders come up last: they read from go2rtc and write to storage, both
    # of which have to be ready first.
    recording = get_recording_manager()
    await recording.start()
    health.register("recording", recording.health_probe, critical=False)

    # Motion last: it reads the same go2rtc substreams and only annotates
    # footage the recorders are already writing.
    motion = get_motion_manager()
    await motion.start()
    health.register("motion", motion.health_probe, critical=False)

    # Exports read segments the recorders have already finished writing, so
    # this only needs storage and the database. Starting it reconciles jobs
    # that a crash left marked "running" -- they become failed, and therefore
    # visible and retryable, instead of hanging for ever.
    exports = get_export_manager()
    await exports.start()

    # Started after everything it observes, and deliberately last among the
    # camera subsystems: it is a reader. If it failed to start, recording would
    # continue unaffected -- only the reporting would be missing.
    reliability = get_reliability_monitor()
    await reliability.start()

    # Announced last, and deliberately allowed to fail: on a network where
    # multicast does not work, nobody notices a missing mDNS record, but
    # everybody notices a server that refused to start.
    await mdns.start()

    logger.info(
        "Sentinel is ready",
        extra={"port": settings.port, "data_dir": str(settings.data_dir)},
    )
    async with session_scope() as session:
        setup_completed = await settings_store.is_setup_completed(session)
    address.announce(settings.port, setup_completed=setup_completed)
    try:
        yield
    finally:
        logger.info("Shutting down Sentinel CCTV")
        health.reset()
        # Reverse of start-up: recorders get their SIGTERM first so FFmpeg can
        # finalise the segment it is writing before its input goes away.
        await mdns.stop()
        await reliability.stop()
        await exports.stop()
        # A cleanup finishes its current small batch before the recorder and
        # database disappear underneath it. Already removed files are never
        # presented as cancellable/undoable.
        await get_cleanup_manager().stop()
        await get_event_cleanup_manager().stop()
        await motion.stop()
        await recording.stop()
        await streaming.stop()
        await ptz.stop()
        vendor_pool.shutdown()
        await dispose_engine()


def _install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        # Starlette's default wording differs from the SPA fallback below.
        # Keep API 404s stable whether or not a built frontend is present.
        if exc.status_code == status.HTTP_404_NOT_FOUND and detail == "Not Found":
            detail = "Not found."
        if isinstance(detail, str):
            detail = redact_text(detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail},
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Echoing the submitted value back would put camera passwords in the
        # response body, so only the location and message are returned.
        errors = [
            {"loc": list(error.get("loc", ())), "msg": redact_text(str(error.get("msg", "")))}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": errors},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled error", extra={"path": request.url.path, "method": request.method}
        )
        del exc  # logged above; never surfaced to the client
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error."},
        )


def _cross_site_cookie_request(request: Request, settings: Settings) -> bool:
    """Detect an unsafe browser request trying to authenticate with a cookie.

    Bearer/API-key clients are not vulnerable to CSRF because a hostile page
    cannot make the browser attach those credentials. Non-browser clients also
    remain usable when they do not send Fetch Metadata or Origin headers.
    """

    if request.method.upper() in _SAFE_HTTP_METHODS:
        return False
    if request.headers.get("authorization") or request.headers.get(_VIEWER_KEY_HEADER):
        return False
    if not (
        request.cookies.get(settings.session_cookie_name)
        or request.cookies.get(_VIEWER_COOKIE_NAME)
    ):
        return False

    fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
    if fetch_site == "cross-site":
        return True

    origin = request.headers.get("origin", "").strip().rstrip("/")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return True
        allowed_origins = {request_origin(request), *settings.cors_origins}
        return origin not in allowed_origins

    # Browsers send at least one of Origin or Sec-Fetch-Site for an unsafe
    # cross-site request. An absent pair is retained for curl, Home Assistant
    # and other direct API clients; explicit same-site without Origin fails shut.
    return fetch_site not in {"", "none", "same-origin"}


def _content_security_policy(settings: Settings) -> str:
    ancestors = list(dict.fromkeys(["'self'", *settings.frame_ancestors, *panel_ancestors()]))
    return "; ".join(
        [
            "default-src 'self'",
            "base-uri 'none'",
            "object-src 'none'",
            f"frame-ancestors {' '.join(ancestors)}",
            "frame-src 'none'",
            "form-action 'self'",
            "script-src 'self'",
            "style-src-elem 'self'",
            "style-src-attr 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "media-src 'self' blob:",
            "font-src 'self'",
            "connect-src 'self' ws: wss:",
            "worker-src 'self' blob:",
            "manifest-src 'self'",
        ]
    )


def _install_security_headers(app: FastAPI, settings: Settings) -> None:
    """Apply browser hardening without breaking Home Assistant embedding."""

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        if _cross_site_cookie_request(request, settings):
            response = JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "Cross-site request blocked."},
            )
        else:
            response = await call_next(request)

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", _content_security_policy(settings))
        if request.url.path.startswith("/api/v1/auth/"):
            response.headers.setdefault("Cache-Control", "no-store")

        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
        if request.url.scheme == "https" or forwarded_proto == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if (
            settings.cookie_secure
            and request.url.path.startswith("/api/v1/viewer/")
            and request.cookies.get("sentinel_cache_v3") != "1"
        ):
            # Early PWA builds could cache the old viewer shell indefinitely.
            # This also covers a 401 before the route handler runs, so an old
            # public viewer can recover even if its grant has expired.
            # Never clear storage here: cookie-blocked HA/mobile embeds repeat
            # this response on every poll. Clearing storage then erases their
            # saved themes and viewer keys. The retirement /sw.js unregisters
            # the obsolete worker without touching local/session storage.
            response.headers.setdefault("Clear-Site-Data", '"cache"')
            response.set_cookie(
                key="sentinel_cache_v3",
                value="1",
                max_age=365 * 24 * 60 * 60,
                httponly=True,
                secure=True,
                samesite="none",
                path="/",
            )
        return response


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built single-page app from the same origin as the API.

    Same-origin serving is why CORS can stay closed by default.
    """
    if not FRONTEND_DIST.is_dir():
        logger.info("No built frontend found; serving the API only")
        return

    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index_file = FRONTEND_DIST / "index.html"
    no_cache = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> Any:
        # An unmatched API or WebSocket path is a genuine 404, not a page route.
        # Without this an API client would get index.html with status 200 and
        # have no way to tell that its endpoint does not exist.
        if full_path in ("api", "ws") or full_path.startswith(("api/", "ws/")):
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not found."}
            )

        # Static file if it exists, otherwise index.html so client-side routes
        # such as /timeline survive a page reload.
        if full_path:
            try:
                candidate = resolve_within(full_path, FRONTEND_DIST)
            except (UnsafePathError, OSError):
                candidate = None
            if candidate is not None and candidate.is_file():
                # The service worker and web manifest must always be
                # revalidated. A long-lived Home Assistant Webpage or iOS PWA
                # otherwise keeps an old API contract after Sentinel upgrades.
                headers = no_cache if candidate.name in {"sw.js", "manifest.webmanifest"} else None
                return FileResponse(candidate, headers=headers)

        if index_file.is_file():
            # HTML points at content-hashed assets, so caching it provides no
            # benefit and can pin an embedded/mobile client to an old build.
            return FileResponse(index_file, headers=no_cache)
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not found."})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="Sentinel CCTV",
        version=__version__,
        summary="Local-first RTSP surveillance with continuous recording",
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_api_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_api_docs else None,
    )
    app.state.settings = settings

    _install_security_headers(app, settings)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )

    _install_exception_handlers(app)
    app.include_router(api_router)
    _mount_frontend(app)
    return app


app = create_app()
