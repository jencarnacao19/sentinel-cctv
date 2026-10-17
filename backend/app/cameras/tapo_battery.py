"""Isolated, event-based provider for Tapo battery cameras.

The provider deliberately does not expose an RTSP URL and is never registered
with go2rtc, the continuous recorder or the motion worker.  It uses pytapo's
proprietary LAN media session only for an explicit live request or clip import.
Vendor payloads are decoded into small typed values; raw identifiers, account
data and session material never leave this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.cameras import tapo, vendor_pool
from app.cameras.probe import ProbeResult, probe_stream
from app.cameras.tapo_stream import DirectStreamNormalisationError, normalise_direct_stream
from app.database.models import Camera, CameraKind
from app.discovery import netsafety
from app.security.crypto import try_decrypt_secret

_NOT_CONFIGURED = "Tapo battery-camera credentials are not configured."
_UNAVAILABLE = "The Tapo battery camera or local hub did not respond."
_AUTH_FAILED = (
    "The camera answered but rejected the saved credentials before sending video. "
    "In Configure, save the current password of the Tapo account that owns this camera. "
    "If Third-Party Compatibility is already enabled, leave it enabled. "
    "A standalone TC82 uses admin for local media authentication; an email cannot fix it."
)
_LOCAL_ONLY = "The Tapo battery camera or hub must use a private local address."

#: Every timeout here is sized to finish inside a reverse proxy's origin window
#: so a slow camera produces a Sentinel error rather than the proxy's own error
#: page. See ``app.cameras.tapo.REQUEST_DEADLINE_SECONDS`` for the same rule on
#: the mains-powered control path.
CONNECT_TIMEOUT_SECONDS = 10.0
LIVE_OPEN_TIMEOUT_SECONDS = 14.0
STATUS_TIMEOUT_SECONDS = 10.0
COMMAND_TIMEOUT_SECONDS = 8.0
#: Listing a week of clips is a bigger answer than a status read, but it is
#: still a foreground request and still has to beat the proxy window.
CLIP_LIST_TIMEOUT_SECONDS = 15.0
CHILD_DISCOVERY_TIMEOUT_SECONDS = 5.0


async def _vendor(func: Callable[[], Any]) -> Any:
    """Run a blocking vendor call on the dedicated pool.

    Never ``asyncio.to_thread``: an abandoned vendor call keeps its worker for
    as long as the operating system keeps its socket, and on the shared default
    executor that starves every other blocking operation in the application.
    """
    return await vendor_pool.run(func, max_workers=tapo.VENDOR_THREAD_LIMIT)


#: How long the Direct Stream may go silent before it is treated as closed.
#: Battery firmware goes quiet when it decides to sleep; waiting longer than
#: this turns "the camera hung up" into "the page is still loading".
LIVE_NO_DATA_TIMEOUT_SECONDS = 15.0


class TapoBatteryError(RuntimeError):
    pass


class TapoBatteryUnsupportedError(TapoBatteryError):
    pass


class TapoBatteryAuthError(TapoBatteryError):
    pass


class TapoBatteryUnavailableError(TapoBatteryError):
    pass


class TapoBatteryBusyError(TapoBatteryError):
    pass


@dataclass(frozen=True, slots=True)
class BatteryTarget:
    camera_id: str
    host: str
    password: str
    child_id: str | None = None
    username: str | None = None
    hub: bool = False

    @property
    def is_standalone(self) -> bool:
        """A blank child id means the host is the camera, not an H200 hub."""
        return not self.hub and not (self.child_id or "").strip()

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for value in (
            self.host,
            self.password,
            self.child_id or "",
            self.username or "",
            "hub" if self.hub else "standalone",
        ):
            digest.update(value.encode("utf-8"))
            digest.update(b"\x00")
        return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class BatteryChildDevice:
    child_id: str
    name: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class BatteryStatus:
    model: str | None
    hardware_version: str | None
    firmware_version: str | None
    battery_percent: int | None
    charging: bool | None
    charging_source: str | None
    read_at: datetime
    children: tuple[BatteryChildDevice, ...] = ()


@dataclass(frozen=True, slots=True)
class RemoteClip:
    external_id: str
    started_at: datetime
    ended_at: datetime
    event_type: str

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.ended_at - self.started_at).total_seconds())


@dataclass(frozen=True, slots=True)
class DownloadedClip:
    path: Path
    probe: ProbeResult


@dataclass(slots=True)
class _Session:
    client: Any
    fingerprint: str


def _safe_error(exc: Exception) -> TapoBatteryError:
    if isinstance(exc, TapoBatteryError):
        return exc
    lowered = str(exc).lower()
    if any(
        marker in lowered
        for marker in (
            "invalid authentication",
            "unauthorized",
            "-40401",
            "device_confirm",
            "device confirm",
            "incorrect password",
            "bad credentials",
            "authentication failed",
            "returned 401 status",
        )
    ):
        return TapoBatteryAuthError(_AUTH_FAILED)
    if any(marker in lowered for marker in ("not supported", "-40106", "-71105")):
        return TapoBatteryUnsupportedError(
            "This firmware did not expose the requested function through the local provider."
        )
    return TapoBatteryUnavailableError(_UNAVAILABLE)


def _extract_children(payload: Any) -> list[BatteryChildDevice]:
    if not isinstance(payload, dict):
        return []
    children: list[BatteryChildDevice] = []
    seen: set[str] = set()

    items: list[Any] = []
    child_control = payload.get("childControl")
    if isinstance(child_control, dict) and isinstance(child_control.get("child_device_list"), list):
        items.extend(child_control["child_device_list"])
    elif isinstance(payload.get("child_device_list"), list):
        items.extend(payload["child_device_list"])
    else:
        for _, value in _walk(payload):
            if isinstance(value, list):
                for candidate in value:
                    if isinstance(candidate, dict) and any(
                        k in candidate for k in ("device_id", "dev_id", "child_id")
                    ):
                        items.append(candidate)

    for item in items:
        if not isinstance(item, dict):
            continue
        dev_id = item.get("device_id") or item.get("dev_id") or item.get("child_id")
        if dev_id is not None and isinstance(dev_id, (str, int)):
            s_id = str(dev_id).strip()
            if s_id and s_id not in seen:
                seen.add(s_id)
                name = (
                    item.get("device_alias")
                    or item.get("device_name")
                    or item.get("alias")
                    or item.get("name")
                )
                model = item.get("device_model") or item.get("model")
                children.append(
                    BatteryChildDevice(
                        child_id=s_id,
                        name=_short(name),
                        model=_short(model),
                    )
                )
    return children


def _walk(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key).lower(), nested
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _first(payloads: Iterable[Any], keys: set[str]) -> Any:
    for payload in payloads:
        for key, value in _walk(payload):
            if key in keys and not isinstance(value, (dict, list)):
                return value
    return None


def _as_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, result))


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"on", "true", "yes", "charging", "1"}:
            return True
        if lowered in {"off", "false", "no", "discharging", "0"}:
            return False
    return None


def _short(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text[:96] if text else None


def _clip_entries(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        lowered = {str(key).lower(): nested for key, nested in value.items()}
        if ({"starttime", "endtime"} <= lowered.keys()) or (
            {"start_time", "end_time"} <= lowered.keys()
        ):
            yield lowered
            return
        for nested in value.values():
            yield from _clip_entries(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _clip_entries(nested)


def decode_clips(payload: Any) -> list[RemoteClip]:
    clips: dict[str, RemoteClip] = {}
    for entry in _clip_entries(payload):
        start_raw = entry.get("starttime", entry.get("start_time"))
        end_raw = entry.get("endtime", entry.get("end_time"))
        if start_raw is None or end_raw is None:
            continue
        try:
            start_ts, end_ts = int(start_raw), int(end_raw)
        except (TypeError, ValueError):
            continue
        if start_ts <= 0 or end_ts <= start_ts:
            continue
        external_id = f"tapo:{start_ts}:{end_ts}"
        raw_event = str(entry.get("event_type", entry.get("type", entry.get("event", "")))).lower()
        event_type = next(
            (
                label
                for marker, label in (
                    ("person", "person"),
                    ("vehicle", "vehicle"),
                    ("pet", "pet"),
                    ("baby", "baby_cry"),
                    ("motion", "motion"),
                )
                if marker in raw_event
            ),
            "camera_event",
        )
        clips[external_id] = RemoteClip(
            external_id=external_id,
            started_at=datetime.fromtimestamp(start_ts, UTC),
            ended_at=datetime.fromtimestamp(end_ts, UTC),
            event_type=event_type,
        )
    return sorted(clips.values(), key=lambda clip: clip.started_at, reverse=True)


def _target_from_camera(camera: Camera) -> BatteryTarget:
    if camera.kind is not CameraKind.TAPO_BATTERY:
        raise TapoBatteryUnsupportedError("This is not a Tapo battery camera.")
    password = try_decrypt_secret(camera.encrypted_tapo_password)
    if not password:
        raise TapoBatteryUnsupportedError(_NOT_CONFIGURED)
    if not camera.host:
        raise TapoBatteryUnsupportedError("The local Tapo host is not configured.")
    return BatteryTarget(
        camera_id=camera.id,
        host=camera.host,
        password=password,
        child_id=camera.tapo_child_id,
        username=camera.username,
        hub=bool(camera.tapo_child_id),
    )


def _connect(target: BatteryTarget) -> Any:
    from pytapo import Tapo

    # If the operator provides an email / username, authenticate using that account
    # identity locally over KLAP; otherwise default to "admin" for local Third-Party
    # Compatibility.
    user = (target.username or "").strip() or "admin"
    return Tapo(
        host=target.host,
        user=user,
        password=target.password,
        cloudPassword=target.password,
        childID=target.child_id,
        controlPort=443,
        streamPort=8800,
        reuseSession=False,
        printWarnInformation=False,
        redactConfidentialInformation=True,
    )


def _standalone_media(target: BatteryTarget, *, encryption_method: str) -> Any:
    """Build a TC82 Direct Stream without first logging in on control port 443.

    Standalone battery cameras expose their on-demand media service on 8800
    while awake, but do not keep pytapo's control endpoint available.  The
    media service performs its own digest authentication and key exchange, so
    constructing a full ``Tapo`` client first only creates a false dependency
    on port 443.
    """
    from app.cameras.tapo_media import StandaloneMediaSession

    return StandaloneMediaSession(
        target.host,
        target.password,
        "",
        encryption_method,
        port=8800,
        username="admin",
    )


async def _open_standalone_media(target: BatteryTarget) -> Any:
    """Authenticate once using the media endpoint's own hash negotiation."""
    from pytapo.const import EncryptionMethod

    media = _standalone_media(target, encryption_method=EncryptionMethod.SHA256)
    try:
        await asyncio.wait_for(media.start(), timeout=LIVE_OPEN_TIMEOUT_SECONDS)
        return media
    except BaseException as exc:
        with suppress(Exception):
            await media.close()
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, Exception):
            raise _safe_error(exc) from exc
        raise


def _standalone_status() -> BatteryStatus:
    """Topology metadata for a standalone camera, without waking or polling it."""
    return BatteryStatus(
        model="TC82",
        hardware_version=None,
        firmware_version=None,
        battery_percent=None,
        charging=None,
        charging_source=None,
        read_at=datetime.now(UTC),
    )


class LiveSession:
    """One opened Direct Stream, ready to be read.

    Created by :meth:`TapoBatteryManager.open_live` after authentication has
    already succeeded, so iterating it can fail for transport reasons but never
    for "the camera said no" -- that answer was given while an HTTP status code
    could still carry it.
    """

    __slots__ = ("_bytes", "_camera_id", "_closed", "_frames", "_manager", "_media", "_request")

    def __init__(
        self,
        *,
        manager: TapoBatteryManager,
        camera_id: str,
        media: Any,
        request: str,
    ) -> None:
        self._manager = manager
        self._camera_id = camera_id
        self._media = media
        self._request = request
        self._bytes = 0
        self._frames = 0
        self._closed = False

    @property
    def bytes_received(self) -> int:
        return self._bytes

    async def chunks(
        self,
        *,
        max_seconds: float,
        ffmpeg_binary: str = "ffmpeg",
        no_data_timeout: float = LIVE_NO_DATA_TIMEOUT_SECONDS,
        stop: asyncio.Event | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Yield local Direct Stream as browser-safe H.264/AAC MPEG-TS."""
        from app.system import privacy

        started = time.monotonic()

        def should_stop() -> bool:
            return bool(
                (stop is not None and stop.is_set())
                or privacy.get_manager().state.enabled
                or time.monotonic() - started >= max_seconds
            )

        try:

            async def checked_responses() -> AsyncIterator[Any]:
                async for response in self._media.transceive(
                    self._request, no_data_timeout=no_data_timeout
                ):
                    payload = getattr(response, "json_data", None)
                    if isinstance(payload, dict):
                        params = payload.get("params")
                        codes = [payload.get("error_code")]
                        if isinstance(params, dict):
                            codes.append(params.get("error_code"))
                        for code in codes:
                            if isinstance(code, int) and code != 0:
                                raise TapoBatteryUnavailableError(
                                    "The camera authenticated but refused to start video "
                                    f"(camera code {code}). Close its other live views and retry."
                                )
                    yield response

            responses = checked_responses()
            async for chunk in normalise_direct_stream(
                responses,
                ffmpeg_binary=ffmpeg_binary,
                should_stop=should_stop,
            ):
                self._bytes += len(chunk)
                self._frames += 1
                yield chunk
        except asyncio.CancelledError:
            raise
        except DirectStreamNormalisationError as exc:
            raise TapoBatteryUnavailableError(str(exc)) from exc
        except Exception as exc:
            raise _safe_error(exc) from exc
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._manager._close_live_media(self._camera_id)


class TapoBatteryManager:
    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._live: set[str] = set()
        self._live_media: dict[str, Any] = {}
        self._live_lock = asyncio.Lock()

    async def close(self, camera_id: str) -> None:
        await self.abort_live(camera_id)
        lock = self._locks.setdefault(camera_id, asyncio.Lock())
        async with lock:
            session = self._sessions.pop(camera_id, None)
            if session is not None:
                with suppress(Exception):
                    await _vendor(session.client.close)
        self._locks.pop(camera_id, None)

    async def close_all(self) -> None:
        await asyncio.gather(
            *(self.abort_live(camera_id) for camera_id in list(self._live)),
            return_exceptions=True,
        )
        for camera_id in list(self._sessions):
            await self.close(camera_id)

    async def _ensure_local(self, target: BatteryTarget) -> None:
        try:
            await netsafety.ensure_scannable_host(target.host)
        except netsafety.UnscannableHostError as exc:
            raise TapoBatteryUnavailableError(_LOCAL_ONLY) from exc

    async def _client(self, target: BatteryTarget) -> Any:
        await self._ensure_local(target)
        session = self._sessions.get(target.camera_id)
        if session is not None and session.fingerprint == target.fingerprint:
            return session.client
        if session is not None:
            with suppress(Exception):
                await _vendor(session.client.close)
        try:
            client = await asyncio.wait_for(
                _vendor(lambda: _connect(target)), timeout=CONNECT_TIMEOUT_SECONDS
            )
        except Exception as exc:
            raise _safe_error(exc) from exc
        self._sessions[target.camera_id] = _Session(client, target.fingerprint)
        return client

    async def test_connection(self, target: BatteryTarget) -> BatteryStatus:
        """One bounded local proof appropriate to the configured topology."""
        lock = self._locks.setdefault(target.camera_id, asyncio.Lock())
        async with lock:
            if target.is_standalone:
                await self._ensure_local(target)
                media: Any | None = None
                try:
                    media = await _open_standalone_media(target)
                except Exception as exc:
                    safe = _safe_error(exc)
                    if isinstance(safe, TapoBatteryUnavailableError):
                        safe = TapoBatteryUnavailableError(
                            "The standalone TC82 did not answer on Direct Stream port 8800. "
                            "Trigger motion to wake it, then try again."
                        )
                    raise safe from exc
                finally:
                    if media is not None:
                        with suppress(Exception):
                            await media.close()
                return _standalone_status()
            client = await self._client(target)
            return await self._status(client)

    async def status(self, camera: Camera) -> BatteryStatus:
        target = _target_from_camera(camera)
        if target.is_standalone:
            # Standby is the normal state. Do not wake the radio, authenticate,
            # or turn a sleeping battery camera into a red system error merely
            # because its dialog was opened.
            return _standalone_status()
        return await self.test_connection(target)

    async def _status(self, client: Any) -> BatteryStatus:
        try:
            battery, statistics = await asyncio.wait_for(
                asyncio.gather(
                    _vendor(client.getBatteryStatus),
                    _vendor(client.getBatteryStatistic),
                    return_exceptions=True,
                ),
                timeout=STATUS_TIMEOUT_SECONDS,
            )
            basic = getattr(client, "basicInfo", {}) or {}
        except Exception as exc:
            raise _safe_error(exc) from exc

        if isinstance(battery, Exception):
            raise _safe_error(battery)
        usable = [basic, battery]
        if not isinstance(statistics, Exception):
            usable.append(statistics)

        children: list[BatteryChildDevice] = []
        if hasattr(client, "getChildDevices"):
            with suppress(Exception):
                child_payload = await asyncio.wait_for(
                    _vendor(client.getChildDevices),
                    timeout=CHILD_DISCOVERY_TIMEOUT_SECONDS,
                )
                if not isinstance(child_payload, Exception):
                    children = _extract_children(child_payload)

        return BatteryStatus(
            model=_short(_first(usable, {"device_model", "model"})),
            hardware_version=_short(_first(usable, {"hw_ver", "hardware_version"})),
            firmware_version=_short(_first(usable, {"sw_ver", "firmware_version"})),
            battery_percent=_as_int(
                _first(usable, {"battery_percent", "battery_percentage", "battery_level"})
            ),
            charging=_as_bool(
                _first(usable, {"charging", "is_charging", "charging_status", "charge_status"})
            ),
            charging_source=_short(
                _first(usable, {"charging_source", "charge_source", "power_source"})
            ),
            read_at=datetime.now(UTC),
            children=tuple(children),
        )

    async def clips(self, camera: Camera, start: datetime, end: datetime) -> list[RemoteClip]:
        target = _target_from_camera(camera)
        lock = self._locks.setdefault(camera.id, asyncio.Lock())
        async with lock:
            client = await self._client(target)
            try:
                lower, upper = int(start.timestamp()), int(end.timestamp())
                payload = await asyncio.wait_for(
                    _vendor(lambda: client.getRecordingsUTC(lower, upper, 0, 999)),
                    timeout=CLIP_LIST_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                raise _safe_error(exc) from exc
        return decode_clips(payload)

    async def download(
        self,
        camera: Camera,
        clip: RemoteClip,
        output: Path,
    ) -> DownloadedClip:
        target = _target_from_camera(camera)
        lock = self._locks.setdefault(camera.id, asyncio.Lock())
        async with lock:
            client = await self._client(target)
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(".part.mp4")
            with suppress(FileNotFoundError):
                temporary.unlink()
            try:
                from pytapo.media_stream.downloader import Downloader

                correction = await asyncio.wait_for(
                    _vendor(client.getTimeCorrection), timeout=COMMAND_TIMEOUT_SECONDS
                )
                downloader = Downloader(
                    client,
                    int(clip.started_at.timestamp()),
                    int(clip.ended_at.timestamp()),
                    int(correction),
                    outputDirectory=f"{temporary.parent}{os.sep}",
                    fileName=temporary.name,
                    stall_timeout=45,
                )
                result = await asyncio.wait_for(downloader.downloadFile(), timeout=180.0)
                if not temporary.is_file() or temporary.stat().st_size < 1024:
                    raise TapoBatteryUnavailableError(
                        "The camera did not return a complete event clip."
                    )
                if str(result.get("currentAction", "")).lower() in {
                    "giving up",
                    "recording in progress",
                }:
                    raise TapoBatteryUnavailableError(
                        "The camera did not return a complete event clip."
                    )
                probe = await probe_stream(str(temporary), timeout_seconds=20.0)
                os.replace(temporary, output)
                return DownloadedClip(path=output, probe=probe)
            except Exception as exc:
                with suppress(FileNotFoundError):
                    temporary.unlink()
                raise _safe_error(exc) from exc

    async def reserve_live(self, camera_id: str) -> None:
        async with self._live_lock:
            if camera_id in self._live:
                raise TapoBatteryBusyError(
                    "This battery camera already has an active Direct Stream session."
                )
            self._live.add(camera_id)

    async def release_live(self, camera_id: str) -> None:
        async with self._live_lock:
            self._live.discard(camera_id)

    async def abort_live(self, camera_id: str) -> None:
        """Close an active media session immediately, notably for privacy mode."""
        media = self._live_media.pop(camera_id, None)
        if media is not None:
            with suppress(Exception):
                await media.close()
        await self.release_live(camera_id)

    def live_active(self, camera_id: str) -> bool:
        return camera_id in self._live

    async def open_live(
        self,
        camera: Camera,
        *,
        quality: Literal["HD", "SD"],
    ) -> LiveSession:
        """Authenticate and open the Direct Stream **before** anything is streamed.

        This split is the whole point of the two-phase API. When the open lived
        inside the streaming generator, the HTTP response had already been
        committed as ``200 OK, Content-Type: video/mp2t`` by the time the camera
        rejected the credentials -- so an authentication failure went out as a
        *successful* response with an empty body. A browser showed a dead
        player; a reverse proxy in front of the origin classified the empty 200
        as a broken upstream and substituted its own error page, which is how a
        camera credential problem came to be reported to the operator as
        "the origin web server returned an invalid or incomplete response".

        Opening first means every failure -- rejected credentials, unreachable
        camera, unsupported firmware -- is raised while the status line can still
        be an honest 4xx/5xx carrying a Sentinel message.
        """
        target = _target_from_camera(camera)
        lock = self._locks.setdefault(camera.id, asyncio.Lock())
        async with lock:
            media: Any | None = None
            try:
                if target.is_standalone:
                    await self._ensure_local(target)
                    media = await _open_standalone_media(target)
                else:
                    client = await self._client(target)
                    from pytapo.media_stream._utils import StreamType

                    media = client.getMediaSession(StreamType.Stream)
                media.set_window_size(50)
                # getMediaSession only constructs an object. Authentication and
                # the HTTP/key-exchange handshake happen in start(), so this has
                # to complete before a StreamingResponse commits 200 OK.
                if not target.is_standalone:
                    await asyncio.wait_for(media.start(), timeout=LIVE_OPEN_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                if media is not None:
                    with suppress(Exception):
                        await media.close()
                raise
            except Exception as exc:
                if media is not None:
                    with suppress(Exception):
                        await media.close()
                raise _safe_error(exc) from exc

        preview: dict[str, Any] = {
            "audio": ["default"],
            "channels": [0],
            "resolutions": [quality],
        }
        if target.child_id:
            preview["deviceId"] = target.child_id
        request = json.dumps(
            {"type": "request", "seq": 1, "params": {"preview": preview, "method": "get"}}
        )
        self._live_media[camera.id] = media
        return LiveSession(manager=self, camera_id=camera.id, media=media, request=request)

    async def _close_live_media(self, camera_id: str) -> None:
        media = self._live_media.pop(camera_id, None)
        if media is not None:
            with suppress(Exception):
                await media.close()


_manager: TapoBatteryManager | None = None


def get_manager() -> TapoBatteryManager:
    global _manager
    if _manager is None:
        _manager = TapoBatteryManager()
    return _manager


def set_manager(manager: TapoBatteryManager | None) -> None:
    global _manager
    _manager = manager
