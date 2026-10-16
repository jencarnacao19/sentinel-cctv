"""Live stream endpoints.

The browser never talks to go2rtc directly. go2rtc listens on loopback with no
authentication of its own, and its admin interface can add arbitrary streams --
including ones pointing at local files. Everything is proxied through Sentinel,
which authenticates the request first and only ever forwards to the one stream
name that belongs to the requested camera.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Annotated

import websockets
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession
from websockets.exceptions import WebSocketException

from app.auth.deps import CurrentPrincipal, get_optional_principal
from app.cameras import service
from app.config import Settings, get_settings
from app.database.models import Camera
from app.database.session import get_session, get_session_factory
from app.logging_config import get_logger
from app.streaming.client import Go2RtcUnavailableError
from app.streaming.config import stream_names
from app.streaming.hls import HlsGrant, hls_sessions
from app.streaming.manager import get_manager
from app.system import privacy
from app.viewer.auth import authorise_viewer, matching_viewer_subprotocol

router = APIRouter(prefix="/streams", tags=["streams"])
logger = get_logger("GO2RTC")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

#: Cap on a single proxied frame. go2rtc's MSE segments are far smaller; this
#: only exists so a malfunctioning peer cannot exhaust memory.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024

#: An SDP offer is a couple of kilobytes at most.
MAX_OFFER_BYTES = 64 * 1024

# Accepted live sockets are tracked so privacy mode can close already-playing
# feeds, not merely reject the next page load.
_ACTIVE_STREAM_SOCKETS: set[WebSocket] = set()

HLS_SESSION_ID = re.compile(rb"hls/playlist\.m3u8\?id=([A-Za-z0-9_-]+)")


async def close_active_streams(reason: str) -> None:
    # Native HLS requests are independent HTTP calls rather than one socket.
    # Clearing their grants makes the next playlist/segment request fail, while
    # the privacy check below protects even an already-copied ticket.
    hls_sessions.clear()
    sockets = list(_ACTIVE_STREAM_SOCKETS)
    if not sockets:
        return
    await asyncio.gather(
        *(socket.close(code=status.WS_1008_POLICY_VIOLATION, reason=reason) for socket in sockets),
        return_exceptions=True,
    )


async def _load_camera(session: AsyncSession, camera_id: str) -> Camera:
    try:
        return await service.get_camera(session, camera_id)
    except service.CameraNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found."
        ) from exc


def _resolve_stream_name(camera: Camera, *, sub: bool) -> str:
    main, sub_name = stream_names(camera)
    return sub_name if (sub and sub_name) else main


@router.get("/{camera_id}", summary="How to play this camera")
async def stream_info(
    camera_id: str, _: CurrentPrincipal, session: SessionDep
) -> dict[str, object]:
    """Everything the player needs, without exposing go2rtc's address."""
    camera = await _load_camera(session, camera_id)
    main, sub = stream_names(camera)
    manager = get_manager()
    return {
        "camera_id": camera.id,
        "enabled": camera.enabled,
        "ready": manager.is_ready,
        "streams": {"main": main, "sub": sub},
        # Relative so the player works over any hostname, port or reverse proxy.
        "websocket_url": f"/api/v1/streams/{camera.id}/ws",
        "snapshot_url": f"/api/v1/cameras/{camera.id}/snapshot",
        "codec": camera.codec,
        "width": camera.width,
        "height": camera.height,
    }


@router.post(
    "/{camera_id}/webrtc",
    summary="Exchange a WebRTC offer for an answer",
    response_class=Response,
)
async def webrtc_offer(
    camera_id: str,
    request: Request,
    _: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    sub: bool = False,
) -> Response:
    """Forward an SDP offer to go2rtc and return its answer.

    Only the camera's own stream name is ever forwarded; the caller cannot
    choose what go2rtc connects to.
    """
    camera = await _load_camera(session, camera_id)
    return await _exchange_webrtc(camera, request, sub=sub)


@router.post(
    "/{camera_id}/viewer-webrtc",
    summary="Exchange a protected viewer WebRTC offer for an answer",
    response_class=Response,
)
async def viewer_webrtc_offer(
    camera_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    sub: bool = False,
) -> Response:
    """WebRTC negotiation for the restricted Home Assistant viewer."""
    await authorise_viewer(request, session, settings)
    camera = await _load_camera(session, camera_id)
    return await _exchange_webrtc(camera, request, sub=sub)


async def _exchange_webrtc(camera: Camera, request: Request, *, sub: bool) -> Response:
    """Forward one already-authorised offer without exposing go2rtc."""
    privacy.require_camera_access()
    if not camera.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This camera is disabled.")

    offer = await request.body()
    if len(offer) > MAX_OFFER_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Offer is too large."
        )

    stream = _resolve_stream_name(camera, sub=sub)
    try:
        answer, content_type = await get_manager().client.webrtc_offer(
            stream,
            offer,
            content_type=request.headers.get("content-type", "application/sdp"),
        )
    except Go2RtcUnavailableError as exc:
        logger.warning("WebRTC negotiation failed", extra={"camera": camera.name, "stream": stream})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The live stream service is not available.",
        ) from exc

    return Response(content=answer, media_type=content_type)


@router.post("/{camera_id}/hls-session", summary="Open a protected native HLS session")
async def hls_session(
    camera_id: str,
    _: CurrentPrincipal,
    session: SessionDep,
    sub: bool = False,
) -> dict[str, str]:
    camera = await _load_camera(session, camera_id)
    return await _open_hls_session(camera, sub=sub)


@router.post(
    "/{camera_id}/viewer-hls-session",
    summary="Open a viewer-scoped native HLS session",
)
async def viewer_hls_session(
    camera_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    sub: bool = False,
) -> dict[str, str]:
    await authorise_viewer(request, session, settings)
    camera = await _load_camera(session, camera_id)
    return await _open_hls_session(camera, sub=sub)


async def _open_hls_session(camera: Camera, *, sub: bool) -> dict[str, str]:
    """Exchange existing authentication for an opaque native-media grant."""
    privacy.require_camera_access()
    if not camera.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This camera is disabled.")

    stream = _resolve_stream_name(camera, sub=sub)
    try:
        master = await get_manager().client.hls_master(stream)
    except Go2RtcUnavailableError as exc:
        logger.warning("HLS session failed", extra={"camera": camera.name, "stream": stream})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The HTTPS live stream is not available.",
        ) from exc

    match = HLS_SESSION_ID.search(master)
    if match is None:
        logger.warning("go2rtc returned an invalid HLS manifest", extra={"camera": camera.name})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The HTTPS live stream returned an invalid manifest.",
        )

    token = hls_sessions.issue(camera.id, match.group(1).decode("ascii"))
    return {
        "url": f"/api/v1/streams/{camera.id}/hls/{token}/playlist.m3u8",
    }


def _hls_grant(camera_id: str, token: str) -> HlsGrant:
    privacy.require_camera_access()
    grant = hls_sessions.resolve(camera_id, token)
    if grant is None:
        # A missing and an expired grant deliberately look identical. Tokens
        # are bearer grants and must not become an enumeration oracle.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stream expired.")
    return grant


def _rewrite_hls_playlist(payload: bytes) -> bytes:
    """Hide go2rtc's session id and keep every native request on Sentinel."""
    text = payload.decode("utf-8")
    text = re.sub(r'init\.mp4\?[^"\s]+', "init.mp4", text)
    text = re.sub(
        r"segment\.m4s\?[^\s]+?&n=(\d+)",
        lambda match: f"segment.m4s?n={match.group(1)}",
        text,
    )
    return text.encode("utf-8")


def _hls_response(content: bytes, content_type: str) -> Response:
    return Response(
        content=content,
        media_type=content_type.split(";", 1)[0],
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/{camera_id}/hls/{token}/playlist.m3u8", include_in_schema=False)
async def hls_playlist(camera_id: str, token: str) -> Response:
    grant = _hls_grant(camera_id, token)
    try:
        content, content_type = await get_manager().client.hls_resource(
            "playlist", grant.upstream_id
        )
    except Go2RtcUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Live playlist unavailable."
        ) from exc
    return _hls_response(_rewrite_hls_playlist(content), content_type)


@router.get("/{camera_id}/hls/{token}/init.mp4", include_in_schema=False)
async def hls_init(camera_id: str, token: str) -> Response:
    grant = _hls_grant(camera_id, token)
    try:
        content, content_type = await get_manager().client.hls_resource("init", grant.upstream_id)
    except Go2RtcUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Live stream unavailable."
        ) from exc
    return _hls_response(content, content_type)


@router.get("/{camera_id}/hls/{token}/segment.m4s", include_in_schema=False)
async def hls_segment(
    camera_id: str,
    token: str,
    n: Annotated[int, Query(ge=0, le=2_147_483_647)],
) -> Response:
    grant = _hls_grant(camera_id, token)
    try:
        content, content_type = await get_manager().client.hls_resource(
            "segment", grant.upstream_id, sequence=n
        )
    except Go2RtcUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Live segment unavailable."
        ) from exc
    return _hls_response(content, content_type)


@router.websocket("/{camera_id}/ws")
async def stream_websocket(
    websocket: WebSocket,
    camera_id: str,
    sub: bool = False,
) -> None:
    """Bidirectional proxy to go2rtc's player socket.

    go2rtc drives both MSE and WebRTC signalling over this one socket, so a
    single proxy covers every playback mode the browser might negotiate.
    """
    settings = get_settings()

    # Authentication happens before the socket is accepted, so an unauthorised
    # client is refused at the handshake and never receives a single frame.
    async with get_session_factory()() as session:
        principal = await get_optional_principal(websocket, session, settings)
        if principal is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorised")
            return

        privacy_state = privacy.get_manager().state
        if privacy_state.enabled:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason=privacy_state.message or "Cameras paused for privacy.",
            )
            return

        camera = await session.get(Camera, camera_id)
        if camera is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unknown camera")
            return
        if not camera.enabled:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Camera disabled")
            return
        stream = _resolve_stream_name(camera, sub=sub)
        camera_name = camera.name

    await _proxy_stream(websocket, settings, stream, camera_name)


@router.websocket("/{camera_id}/viewer-ws")
async def viewer_stream_websocket(
    websocket: WebSocket,
    camera_id: str,
    sub: bool = False,
) -> None:
    """Read-only live stream for the viewer surface.

    Viewer authorisation is intentionally not a ``Principal`` and therefore
    cannot be reused against camera, storage or settings endpoints.
    """
    settings = get_settings()
    selected_subprotocol = matching_viewer_subprotocol(websocket, settings)

    async with get_session_factory()() as session:
        try:
            await authorise_viewer(websocket, session, settings)
        except HTTPException:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorised")
            return

        privacy_state = privacy.get_manager().state
        if privacy_state.enabled:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason=privacy_state.message or "Cameras paused for privacy.",
            )
            return

        camera = await session.get(Camera, camera_id)
        if camera is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unknown camera")
            return
        if not camera.enabled:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Camera disabled")
            return
        stream = _resolve_stream_name(camera, sub=sub)
        camera_name = camera.name

    await _proxy_stream(
        websocket,
        settings,
        stream,
        camera_name,
        subprotocol=selected_subprotocol,
    )


async def _proxy_stream(
    websocket: WebSocket,
    settings: Settings,
    stream: str,
    camera_name: str,
    *,
    subprotocol: str | None = None,
) -> None:
    """Proxy one already-authorised camera stream to go2rtc."""
    upstream_url = f"ws://{settings.go2rtc_host}:{settings.go2rtc_api_port}/api/ws?src={stream}"

    await websocket.accept(subprotocol=subprotocol)
    _ACTIVE_STREAM_SOCKETS.add(websocket)
    try:
        async with websockets.connect(
            upstream_url, max_size=MAX_MESSAGE_BYTES, open_timeout=10
        ) as upstream:
            await _pump(websocket, upstream, camera_name)
    except (OSError, WebSocketException, TimeoutError):
        logger.info(
            "Could not reach go2rtc for live view",
            extra={"camera": camera_name, "stream": stream},
        )
        with contextlib.suppress(RuntimeError):
            await websocket.close(
                code=status.WS_1011_INTERNAL_ERROR, reason="Live stream unavailable"
            )
    except WebSocketDisconnect:
        pass
    finally:
        _ACTIVE_STREAM_SOCKETS.discard(websocket)


async def _pump(client: WebSocket, upstream: websockets.ClientConnection, camera_name: str) -> None:
    """Copy messages in both directions until either side closes."""

    async def client_to_upstream() -> None:
        while True:
            message = await client.receive()
            if message["type"] == "websocket.disconnect":
                return
            if (text := message.get("text")) is not None:
                await upstream.send(text)
            elif (data := message.get("bytes")) is not None:
                await upstream.send(data)

    async def upstream_to_client() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                await client.send_bytes(message)
            else:
                await client.send_text(message)

    tasks = [
        asyncio.create_task(client_to_upstream(), name=f"ws-up:{camera_name}"),
        asyncio.create_task(upstream_to_client(), name=f"ws-down:{camera_name}"),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            # Surface a genuine error; a closed socket on either side is normal.
            with contextlib.suppress(
                asyncio.CancelledError, WebSocketDisconnect, WebSocketException, RuntimeError
            ):
                task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.get("/{camera_id}/status", summary="go2rtc process and stream status")
async def stream_status(
    camera_id: str, _: CurrentPrincipal, session: SessionDep
) -> dict[str, object]:
    camera = await _load_camera(session, camera_id)
    manager = get_manager()
    main, sub = stream_names(camera)

    producers: object = None
    try:
        streams = await manager.client.list_streams()
        producers = streams.get(main)
    except Go2RtcUnavailableError:
        producers = None

    return {
        "camera_id": camera.id,
        "ready": manager.is_ready,
        "streams": {"main": main, "sub": sub},
        "go2rtc": manager.process_status(),
        "known_to_go2rtc": producers is not None,
    }
