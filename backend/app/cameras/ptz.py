"""Low-latency ONVIF pan/tilt/zoom control.

Capability discovery is deliberately separate from movement. The service and
profile token are persisted once, so a button press is one small SOAP request
over a pooled LAN connection rather than a fresh ONVIF discovery flow.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx

from app.cameras import tapo, vendor_pool
from app.database.models import Camera
from app.discovery import netsafety, onvif, xmlsafe
from app.logging_config import get_logger
from app.security.crypto import try_decrypt_secret

logger = get_logger("CAMERA")


async def _vendor(func: Callable[[], Any]) -> Any:
    """Run a blocking pytapo call on the dedicated vendor pool.

    Never ``asyncio.to_thread``. Constructing a ``Tapo`` client performs a
    network round trip that cannot be cancelled, so on the interpreter's shared
    executor a camera that has stopped answering holds one of its twelve workers
    until the operating system gives up on the socket -- and every other
    blocking operation in the application queues behind it.

    That matters most on exactly this path. Patrol restoration runs against a
    camera whose stored Tapo credentials may be rejected, and it retries every
    thirty minutes, so a single misconfigured camera was a recurring source of
    the measured 1800x slowdown of unrelated requests.
    """
    return await vendor_pool.run(func, max_workers=tapo.VENDOR_THREAD_LIMIT)


PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
SCHEMA_NS = "http://www.onvif.org/ver10/schema"
DEAD_MAN_SECONDS = 1.1
PATROL_RESUME_DELAY_SECONDS = 0.35
PATROL_FAILURE_BACKOFF_SECONDS = 60.0
PATROL_AUTH_BACKOFF_SECONDS = 30 * 60.0
VENDOR_PATROL_TIMEOUT_SECONDS = 14.0


class PtzError(RuntimeError):
    """A safe, user-facing PTZ failure."""


class PtzNotConfiguredError(PtzError):
    pass


@dataclass(frozen=True, slots=True)
class PtzCapability:
    supported: bool
    message: str
    device_xaddr: str
    ptz_xaddr: str | None = None
    profile_token: str | None = None
    zoom_supported: bool = False
    preset_capacity: int = 0
    preset_tour_capacity: int = 0
    home_supported: bool = False


@dataclass(frozen=True, slots=True)
class PtzPreset:
    token: str
    name: str


@dataclass(frozen=True, slots=True)
class PtzNodeCapabilities:
    preset_capacity: int = 0
    preset_tour_capacity: int = 0
    home_supported: bool = False


@dataclass(frozen=True, slots=True)
class _ControlTarget:
    camera_id: str
    camera_name: str
    xaddr: str
    profile_token: str
    zoom_supported: bool
    username: str | None
    password: str | None
    tapo_username: str | None
    tapo_password: str | None
    tapo_patrol_mode: str | None
    onvif_patrol_tour_token: str | None


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _service_xaddr(root: ET.Element, service: str) -> str | None:
    element = xmlsafe.find_first(root, service)
    return _text(xmlsafe.find_first(element, "XAddr"))


def _pin_service_to_camera(url: str, camera_host: str) -> str:
    """Retain the advertised port/path but never follow it to another host."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PtzError("The camera returned an invalid ONVIF service address.")
    host = camera_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def _profile_tokens(root: ET.Element) -> tuple[list[str], dict[str, str]]:
    all_tokens: list[str] = []
    ptz_configurations: dict[str, str] = {}
    for profile in xmlsafe.find_all(root, "Profiles"):
        token = profile.get("token")
        if not token:
            continue
        all_tokens.append(token)
        configuration = xmlsafe.find_first(profile, "PTZConfiguration")
        configuration_token = configuration.get("token") if configuration is not None else None
        if configuration_token:
            ptz_configurations[token] = configuration_token
    return all_tokens, ptz_configurations


def _stream_signature(uri: str | None) -> tuple[str, str]:
    if not uri:
        return "", ""
    parts = urlsplit(uri)
    return parts.path.rstrip("/"), parts.query


def _first_int(root: ET.Element, name: str) -> int:
    value = _text(xmlsafe.find_first(root, name))
    try:
        return max(0, int(value or "0"))
    except ValueError:
        return 0


def _first_bool(root: ET.Element, name: str) -> bool:
    value = _text(xmlsafe.find_first(root, name))
    return value is not None and value.lower() in {"true", "1"}


class PtzManager:
    """Own the pooled HTTP client and one dead-man timer per moving camera."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._dead_men: dict[str, asyncio.Task[None]] = {}
        self._patrol_tasks: set[asyncio.Task[None]] = set()
        self._tapo_clients: dict[str, Any] = {}
        self._patrol_retry_after: dict[str, float] = {}
        self._versions: dict[str, int] = {}

    async def start(self) -> None:
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(3.0, connect=1.5),
            # Several consumer ONVIF implementations (including Tapo TC46/
            # TC71) advertise HTTP/1.1 keep-alive but corrupt the next response
            # when the socket is reused. A fresh LAN connection costs only a
            # few milliseconds and is substantially safer for movement/Stop.
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=0),
            headers={"Connection": "close"},
        )

    async def stop(self) -> None:
        tasks = list(self._dead_men.values())
        self._dead_men.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        # Patrol calls are deliberately not cancelled once dispatched to a
        # worker thread. Waiting avoids closing a cached Tapo session while
        # that same session is still restoring cruise mode.
        if self._patrol_tasks:
            await asyncio.gather(*list(self._patrol_tasks), return_exceptions=True)
        tapo_clients = list(self._tapo_clients.values())
        self._tapo_clients.clear()
        for tapo_client in tapo_clients:
            with suppress(Exception):
                await _vendor(tapo_client.close)
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._locks.clear()
        self._patrol_retry_after.clear()
        self._versions.clear()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            await self.start()
        client = self._client
        if client is None:  # pragma: no cover - start() always assigns it
            raise RuntimeError("PTZ HTTP client did not start.")
        return client

    async def detect(self, camera: Camera, *, device_xaddr: str | None = None) -> PtzCapability:
        """Locate and validate PTZ without moving the camera."""
        try:
            await netsafety.ensure_scannable_host(camera.host)
        except netsafety.UnscannableHostError as exc:
            raise PtzError(str(exc)) from exc
        candidates = [
            device_xaddr,
            camera.onvif_device_xaddr,
            f"http://{camera.host}:2020/onvif/device_service",
            f"http://{camera.host}/onvif/device_service",
        ]
        unique = list(dict.fromkeys(candidate for candidate in candidates if candidate))
        password = try_decrypt_secret(camera.encrypted_password)
        last_error: Exception | None = None
        for candidate in unique:
            try:
                pinned = _pin_service_to_camera(candidate, camera.host)
                return await self._detect_at(
                    camera,
                    pinned,
                    username=camera.username,
                    password=password,
                )
            except (onvif.OnvifError, PtzError, httpx.HTTPError) as exc:
                last_error = exc
                logger.debug(
                    "ONVIF PTZ candidate did not answer",
                    extra={"camera_id": camera.id, "host": camera.host, "error": str(exc)},
                )
        detail = f" {last_error}" if last_error is not None else ""
        raise PtzError(f"No ONVIF control service answered for this camera.{detail}")

    async def identify(
        self, camera: Camera, *, device_xaddr: str | None = None
    ) -> onvif.DeviceIdentity:
        """Ask the camera who made it, without changing anything on it.

        Shares the PTZ manager's connection pool and its candidate-address list
        because it is the same conversation with the same service; a separate
        client would mean a second handshake for one extra question.
        """
        try:
            await netsafety.ensure_scannable_host(camera.host)
        except netsafety.UnscannableHostError as exc:
            raise PtzError(str(exc)) from exc

        candidates = [
            device_xaddr,
            camera.onvif_device_xaddr,
            f"http://{camera.host}:2020/onvif/device_service",
            f"http://{camera.host}/onvif/device_service",
        ]
        unique = list(dict.fromkeys(candidate for candidate in candidates if candidate))
        password = try_decrypt_secret(camera.encrypted_password)
        client = await self._get_client()
        last_error: Exception | None = None
        for candidate in unique:
            try:
                pinned = _pin_service_to_camera(candidate, camera.host)
                return await onvif.get_device_information(client, pinned, camera.username, password)
            except (onvif.OnvifError, PtzError, httpx.HTTPError) as exc:
                last_error = exc
                logger.debug(
                    "ONVIF identity candidate did not answer",
                    extra={"camera_id": camera.id, "host": camera.host, "error": str(exc)},
                )
        detail = f" {last_error}" if last_error is not None else ""
        raise PtzError(f"No ONVIF device service answered for this camera.{detail}")

    async def _detect_at(
        self,
        camera: Camera,
        device_xaddr: str,
        *,
        username: str | None,
        password: str | None,
    ) -> PtzCapability:
        client = await self._get_client()
        root = await onvif._soap_post(
            client,
            device_xaddr,
            action=f"{onvif._DEVICE_NS}/GetCapabilities",
            body=(
                f'<GetCapabilities xmlns="{onvif._DEVICE_NS}">'
                "<Category>All</Category>"
                "</GetCapabilities>"
            ),
            username=username,
            password=password,
        )
        advertised_ptz = _service_xaddr(root, "PTZ")
        if not advertised_ptz:
            return PtzCapability(
                supported=False,
                message="This camera does not advertise ONVIF PTZ control.",
                device_xaddr=device_xaddr,
            )

        ptz_xaddr = _pin_service_to_camera(advertised_ptz, camera.host)
        advertised_media = _service_xaddr(root, "Media")
        media_xaddr = _pin_service_to_camera(
            advertised_media or device_xaddr,
            camera.host,
        )
        profiles_root = await onvif._soap_post(
            client,
            media_xaddr,
            action=f"{onvif._MEDIA_NS}/GetProfiles",
            body=f'<GetProfiles xmlns="{onvif._MEDIA_NS}"/>',
            username=username,
            password=password,
        )
        all_tokens, ptz_configurations = _profile_tokens(profiles_root)
        preferred = camera.onvif_profile_token
        controllable_tokens = list(ptz_configurations) or all_tokens
        profile_token: str | None = None
        if preferred and preferred in controllable_tokens:
            profile_token = preferred
        elif preferred and preferred in all_tokens:
            # A few incomplete ONVIF stacks expose a PTZ service but omit the
            # PTZConfiguration element. The selected media profile still works.
            profile_token = preferred
        else:
            # A physical camera normally offers several quality profiles; an
            # NVR offers profiles for several *different channels*. Match the
            # selected RTSP path instead of blindly making every NVR row control
            # its first channel.
            expected_stream = _stream_signature(camera.main_stream_url)
            for token in controllable_tokens[:64]:
                try:
                    uri = await onvif.get_stream_uri(
                        client,
                        media_xaddr,
                        username,
                        password,
                        token,
                    )
                except onvif.OnvifError:
                    continue
                if _stream_signature(uri) == expected_stream:
                    profile_token = token
                    break
            if profile_token is None and len(controllable_tokens) == 1:
                profile_token = controllable_tokens[0]
        if profile_token is None:
            return PtzCapability(
                supported=False,
                message=(
                    "The PTZ service is present, but its media profile could not be safely "
                    "matched to this camera channel."
                ),
                device_xaddr=device_xaddr,
            )
        pan_tilt_supported = True
        zoom_supported = False
        configuration_token = ptz_configurations.get(profile_token)
        if configuration_token:
            try:
                options_root = await onvif._soap_post(
                    client,
                    ptz_xaddr,
                    action=f"{PTZ_NS}/GetConfigurationOptions",
                    body=(
                        f'<GetConfigurationOptions xmlns="{PTZ_NS}">'
                        f"<ConfigurationToken>{escape(configuration_token)}</ConfigurationToken>"
                        "</GetConfigurationOptions>"
                    ),
                    username=username,
                    password=password,
                )
                pan_tilt_supported = (
                    xmlsafe.find_first(options_root, "ContinuousPanTiltVelocitySpace") is not None
                )
                zoom_supported = (
                    xmlsafe.find_first(options_root, "ContinuousZoomVelocitySpace") is not None
                )
            except onvif.OnvifError:
                # The associated PTZConfiguration is enough to keep pan/tilt
                # usable on incomplete devices; never guess optional zoom.
                pan_tilt_supported = True
                zoom_supported = False
        node_capabilities = PtzNodeCapabilities()
        try:
            node_capabilities = await self._get_node_capabilities(
                ptz_xaddr,
                username=username,
                password=password,
            )
        except PtzError:
            # GetNodes is optional on several incomplete consumer ONVIF stacks.
            # Movement remains usable even when richer preset metadata is absent.
            logger.debug(
                "ONVIF PTZ node metadata is unavailable",
                extra={"camera_id": camera.id, "camera": camera.name},
            )
        if not pan_tilt_supported:
            return PtzCapability(
                supported=False,
                message="This camera does not expose continuous pan/tilt control.",
                device_xaddr=device_xaddr,
            )
        return PtzCapability(
            supported=True,
            message="ONVIF pan/tilt and zoom are ready."
            if zoom_supported
            else "ONVIF pan/tilt is ready.",
            device_xaddr=device_xaddr,
            ptz_xaddr=ptz_xaddr,
            profile_token=profile_token,
            zoom_supported=zoom_supported,
            preset_capacity=node_capabilities.preset_capacity,
            preset_tour_capacity=node_capabilities.preset_tour_capacity,
            home_supported=node_capabilities.home_supported,
        )

    async def _get_node_capabilities(
        self,
        xaddr: str,
        *,
        username: str | None,
        password: str | None,
    ) -> PtzNodeCapabilities:
        client = await self._get_client()
        try:
            root = await onvif._soap_post(
                client,
                xaddr,
                action=f"{PTZ_NS}/GetNodes",
                body=f'<GetNodes xmlns="{PTZ_NS}"/>',
                username=username,
                password=password,
            )
        except onvif.OnvifError as exc:
            raise PtzError(str(exc)) from exc
        return PtzNodeCapabilities(
            preset_capacity=_first_int(root, "MaximumNumberOfPresets"),
            preset_tour_capacity=_first_int(root, "MaximumNumberOfPresetTours"),
            home_supported=_first_bool(root, "HomeSupported"),
        )

    async def get_node_capabilities(self, camera: Camera) -> PtzNodeCapabilities:
        target = self._target(camera)
        return await self._get_node_capabilities(
            target.xaddr,
            username=target.username,
            password=target.password,
        )

    async def list_presets(self, camera: Camera) -> list[PtzPreset]:
        target = self._target(camera)
        root = await self._request(
            target,
            "GetPresets",
            f'<GetPresets xmlns="{PTZ_NS}">'
            f"<ProfileToken>{escape(target.profile_token)}</ProfileToken>"
            "</GetPresets>",
        )
        presets: list[PtzPreset] = []
        for element in xmlsafe.find_all(root, "Preset"):
            token = element.get("token")
            if not token:
                continue
            name = _text(xmlsafe.find_first(element, "Name")) or f"Preset {token}"
            presets.append(PtzPreset(token=token, name=name))
        return presets

    async def set_preset(self, camera: Camera, name: str) -> PtzPreset:
        target = self._target(camera)
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 64:
            raise PtzError("Preset names must contain between 1 and 64 characters.")
        root = await self._request(
            target,
            "SetPreset",
            f'<SetPreset xmlns="{PTZ_NS}">'
            f"<ProfileToken>{escape(target.profile_token)}</ProfileToken>"
            f"<PresetName>{escape(clean_name)}</PresetName>"
            "</SetPreset>",
        )
        token = _text(xmlsafe.find_first(root, "PresetToken"))
        if not token:
            raise PtzError("The camera saved the preset but returned no preset token.")
        return PtzPreset(token=token, name=clean_name)

    async def remove_preset(self, camera: Camera, token: str) -> None:
        target = self._target(camera)
        if not token or len(token) > 512:
            raise PtzError("The preset token is invalid.")
        await self._request(
            target,
            "RemovePreset",
            f'<RemovePreset xmlns="{PTZ_NS}">'
            f"<ProfileToken>{escape(target.profile_token)}</ProfileToken>"
            f"<PresetToken>{escape(token)}</PresetToken>"
            "</RemovePreset>",
        )

    async def goto_preset(self, camera: Camera, token: str) -> None:
        target = self._target(camera)
        if not token or len(token) > 512:
            raise PtzError("The preset token is invalid.")
        self._versions[camera.id] = self._versions.get(camera.id, 0) + 1
        self._cancel_dead_man(camera.id)
        lock = self._locks.setdefault(camera.id, asyncio.Lock())
        async with lock:
            await self._request(
                target,
                "GotoPreset",
                f'<GotoPreset xmlns="{PTZ_NS}">'
                f"<ProfileToken>{escape(target.profile_token)}</ProfileToken>"
                f"<PresetToken>{escape(token)}</PresetToken>"
                "</GotoPreset>",
            )

    async def configure_native_patrol(
        self,
        camera: Camera,
        *,
        preset_tokens: list[str],
        stay_seconds: int,
        name: str,
    ) -> str:
        target = self._target(camera)
        capabilities = await self.get_node_capabilities(camera)
        if capabilities.preset_tour_capacity < 1:
            raise PtzNotConfiguredError("This camera does not expose ONVIF preset tours.")
        available = {preset.token for preset in await self.list_presets(camera)}
        if len(preset_tokens) < 2 or any(token not in available for token in preset_tokens):
            raise PtzError("Select at least two presets that still exist on this camera.")
        if not 2 <= stay_seconds <= 300:
            raise PtzError("Patrol stay time must be between 2 and 300 seconds.")

        clean_name = name.strip()
        if not clean_name or len(clean_name) > 64:
            raise PtzError("Patrol names must contain between 1 and 64 characters.")
        tour_token = camera.onvif_patrol_tour_token
        created = False
        # Continuous movement and proprietary cruise modes can prevent an
        # ONVIF tour from activating. Stop first, but do not schedule the old
        # patrol to resume while it is being replaced.
        lock = self._locks.setdefault(camera.id, asyncio.Lock())
        async with lock:
            await self._send_stop(target)
        if not tour_token:
            root = await self._request(
                target,
                "CreatePresetTour",
                f'<CreatePresetTour xmlns="{PTZ_NS}">'
                f"<ProfileToken>{escape(target.profile_token)}</ProfileToken>"
                "</CreatePresetTour>",
            )
            tour_token = _text(xmlsafe.find_first(root, "PresetTourToken"))
            if not tour_token:
                raise PtzError("The camera created no usable ONVIF patrol token.")
            created = True

        spots = "".join(
            "<tt:TourSpot>"
            "<tt:PresetDetail>"
            f"<tt:PresetToken>{escape(token)}</tt:PresetToken>"
            "</tt:PresetDetail>"
            f"<tt:StayTime>PT{stay_seconds}S</tt:StayTime>"
            "</tt:TourSpot>"
            for token in preset_tokens
        )
        try:
            await self._request(
                target,
                "ModifyPresetTour",
                f'<ModifyPresetTour xmlns="{PTZ_NS}" xmlns:tt="{SCHEMA_NS}">'
                f"<ProfileToken>{escape(target.profile_token)}</ProfileToken>"
                f'<PresetTour token="{escape(tour_token)}">'
                f"<tt:Name>{escape(clean_name)}</tt:Name>"
                f"{spots}"
                "</PresetTour>"
                "</ModifyPresetTour>",
            )
            await self._operate_native_tour(target, tour_token, "Start")
        except PtzError:
            if created:
                with suppress(PtzError):
                    await self._remove_native_tour(target, tour_token)
            if target.tapo_patrol_mode:
                with suppress(Exception):
                    await asyncio.wait_for(
                        _vendor(lambda: self._activate_tapo_patrol_sync(target)),
                        timeout=VENDOR_PATROL_TIMEOUT_SECONDS,
                    )
            raise
        return tour_token

    async def disable_patrol(self, camera: Camera) -> None:
        target = self._target(camera)
        token = camera.onvif_patrol_tour_token
        if token:
            await self._operate_native_tour(target, token, "Stop")
        if target.tapo_patrol_mode:
            # Proprietary shutdown is best-effort. It must never make an ONVIF
            # stop fail or prevent the local configuration from being cleared.
            try:
                await asyncio.wait_for(
                    _vendor(lambda: self._set_tapo_patrol_sync(target, False)),
                    timeout=VENDOR_PATROL_TIMEOUT_SECONDS,
                )
            except Exception:
                logger.warning(
                    "Tapo LAN patrol could not be stopped",
                    extra={"camera_id": target.camera_id, "camera": target.camera_name},
                    exc_info=True,
                )

    async def _request(self, target: _ControlTarget, method: str, body: str) -> ET.Element:
        client = await self._get_client()
        try:
            return await onvif._soap_post(
                client,
                target.xaddr,
                action=f"{PTZ_NS}/{method}",
                body=body,
                username=target.username,
                password=target.password,
            )
        except onvif.OnvifError as exc:
            raise PtzError(str(exc)) from exc

    async def _operate_native_tour(
        self,
        target: _ControlTarget,
        tour_token: str,
        operation: str,
    ) -> None:
        await self._request(
            target,
            "OperatePresetTour",
            f'<OperatePresetTour xmlns="{PTZ_NS}">'
            f"<ProfileToken>{escape(target.profile_token)}</ProfileToken>"
            f"<PresetTourToken>{escape(tour_token)}</PresetTourToken>"
            f"<Operation>{escape(operation)}</Operation>"
            "</OperatePresetTour>",
        )

    async def _remove_native_tour(self, target: _ControlTarget, tour_token: str) -> None:
        await self._request(
            target,
            "RemovePresetTour",
            f'<RemovePresetTour xmlns="{PTZ_NS}">'
            f"<ProfileToken>{escape(target.profile_token)}</ProfileToken>"
            f"<PresetTourToken>{escape(tour_token)}</PresetTourToken>"
            "</RemovePresetTour>",
        )

    @staticmethod
    def _target(camera: Camera) -> _ControlTarget:
        if (
            camera.ptz_supported is not True
            or not camera.onvif_ptz_xaddr
            or not camera.onvif_profile_token
        ):
            raise PtzNotConfiguredError("PTZ is not available for this camera.")
        password = try_decrypt_secret(camera.encrypted_password)
        tapo_password = try_decrypt_secret(camera.encrypted_tapo_password)
        return _ControlTarget(
            camera_id=camera.id,
            camera_name=camera.name,
            xaddr=camera.onvif_ptz_xaddr,
            profile_token=camera.onvif_profile_token,
            zoom_supported=camera.ptz_zoom_supported,
            username=camera.username,
            password=password,
            tapo_username="admin" if tapo_password else camera.username,
            tapo_password=tapo_password or password,
            tapo_patrol_mode=camera.tapo_patrol_mode,
            onvif_patrol_tour_token=camera.onvif_patrol_tour_token,
        )

    def _set_tapo_patrol_sync(self, target: _ControlTarget, enabled: bool) -> None:
        """Set proprietary Tapo cruise mode directly on the LAN."""
        if not target.tapo_username or not target.tapo_password or not target.tapo_patrol_mode:
            return
        coord = "x" if target.tapo_patrol_mode == "horizontal" else "y"
        tapo_client = self._tapo_clients.get(target.camera_id)
        if tapo_client is not None and (
            tapo_client.host != urlsplit(target.xaddr).hostname
            or tapo_client.user != target.tapo_username
            or tapo_client.password != target.tapo_password
        ):
            with suppress(Exception):
                tapo_client.close()
            tapo_client = None
            self._tapo_clients.pop(target.camera_id, None)
        if tapo_client is None:
            # Imported lazily so ONVIF-only installations do not initialise
            # the vendor client at process startup.
            from pytapo import Tapo

            host = urlsplit(target.xaddr).hostname
            if host is None:  # already validated by _target(), defensive only
                raise PtzError("The Tapo camera address is invalid.")
            tapo_client = Tapo(
                host,
                target.tapo_username,
                target.tapo_password,
                reuseSession=True,
                printWarnInformation=False,
            )
            self._tapo_clients[target.camera_id] = tapo_client
        tapo_client.setCruise(enabled, coord=coord if enabled else False)

    def _activate_tapo_patrol_sync(self, target: _ControlTarget) -> None:
        """Restore proprietary Tapo cruise mode in a worker thread."""
        self._set_tapo_patrol_sync(target, True)

    def _schedule_patrol_resume(self, target: _ControlTarget, version: int) -> None:
        if not target.onvif_patrol_tour_token and not target.tapo_patrol_mode:
            return
        loop = asyncio.get_running_loop()
        retry_after = self._patrol_retry_after.get(target.camera_id, 0.0)
        if retry_after > loop.time():
            logger.debug(
                "Tapo patrol retry suppressed during backoff",
                extra={"camera_id": target.camera_id, "camera": target.camera_name},
            )
            return
        self._patrol_retry_after.pop(target.camera_id, None)
        task = asyncio.create_task(
            self._resume_patrol(target, version),
            name=f"ptz-patrol-resume-{target.camera_id}",
        )
        self._patrol_tasks.add(task)
        task.add_done_callback(self._patrol_tasks.discard)

    async def _resume_patrol(self, target: _ControlTarget, version: int) -> None:
        resumed_backend = "onvif_lan" if target.onvif_patrol_tour_token else "tapo_lan"
        try:
            await asyncio.sleep(PATROL_RESUME_DELAY_SECONDS)
            lock = self._locks.setdefault(target.camera_id, asyncio.Lock())
            async with lock:
                # A newer move or stop owns the camera now. That command will
                # arrange its own patrol restoration when it is finished.
                if self._versions.get(target.camera_id) != version:
                    return
                if target.onvif_patrol_tour_token:
                    try:
                        await self._operate_native_tour(
                            target,
                            target.onvif_patrol_tour_token,
                            "Start",
                        )
                    except PtzError:
                        if not target.tapo_patrol_mode:
                            raise
                        logger.warning(
                            "ONVIF patrol resume failed; using the configured Tapo LAN fallback",
                            extra={"camera_id": target.camera_id, "camera": target.camera_name},
                        )
                        await asyncio.wait_for(
                            _vendor(lambda: self._activate_tapo_patrol_sync(target)),
                            timeout=VENDOR_PATROL_TIMEOUT_SECONDS,
                        )
                        resumed_backend = "tapo_lan"
                else:
                    await asyncio.wait_for(
                        _vendor(lambda: self._activate_tapo_patrol_sync(target)),
                        timeout=VENDOR_PATROL_TIMEOUT_SECONDS,
                    )
                self._patrol_retry_after.pop(target.camera_id, None)
            logger.info(
                "Local patrol restored after manual PTZ",
                extra={
                    "camera_id": target.camera_id,
                    "camera": target.camera_name,
                    "patrol_backend": resumed_backend,
                },
            )
        except Exception as exc:
            # Stop is safety-critical and has already succeeded. A proprietary
            # patrol failure must stay visible without turning that Stop into a
            # misleading API error or moving the camera again.
            failed_client = self._tapo_clients.pop(target.camera_id, None)
            if failed_client is not None:
                with suppress(Exception):
                    await _vendor(failed_client.close)
            message = str(exc).lower()
            auth_failure = "authentication" in message or "suspension" in message
            backoff_seconds = (
                PATROL_AUTH_BACKOFF_SECONDS if auth_failure else PATROL_FAILURE_BACKOFF_SECONDS
            )
            self._patrol_retry_after[target.camera_id] = (
                asyncio.get_running_loop().time() + backoff_seconds
            )
            logger.warning(
                "Local patrol could not be restored after manual PTZ",
                extra={
                    "camera_id": target.camera_id,
                    "camera": target.camera_name,
                    "retry_after_seconds": int(backoff_seconds),
                },
                exc_info=True,
            )

    def _cancel_dead_man(self, camera_id: str) -> None:
        task = self._dead_men.pop(camera_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def move(self, camera: Camera, *, pan: float, tilt: float, zoom: float) -> None:
        target = self._target(camera)
        moving_pan_tilt = abs(pan) >= 0.001 or abs(tilt) >= 0.001
        moving_zoom = abs(zoom) >= 0.001
        if moving_zoom and not target.zoom_supported:
            raise PtzNotConfiguredError("Zoom is not available for this camera.")
        if not moving_pan_tilt and not moving_zoom:
            raise PtzError("At least one PTZ velocity must be non-zero.")
        client = await self._get_client()
        lock = self._locks.setdefault(camera.id, asyncio.Lock())
        version = self._versions.get(camera.id, 0) + 1
        self._versions[camera.id] = version
        velocities = ""
        if moving_pan_tilt:
            velocities += f'<PanTilt xmlns="{SCHEMA_NS}" x="{pan:.3f}" y="{tilt:.3f}"/>'
        if moving_zoom:
            velocities += f'<Zoom xmlns="{SCHEMA_NS}" x="{zoom:.3f}"/>'
        body = (
            f'<ContinuousMove xmlns="{PTZ_NS}">'
            f"<ProfileToken>{escape(target.profile_token)}</ProfileToken>"
            f"<Velocity>{velocities}</Velocity>"
            # Timeout is optional in ONVIF. Tapo TC46/TC71 return a malformed
            # HTTP 500 when it is present, so Sentinel's independent dead-man
            # timer below provides the safety cutoff instead.
            "</ContinuousMove>"
        )
        async with lock:
            # Several 450 ms renewals can be in flight on a slow camera. A stop
            # increments the version before waiting for this lock, making every
            # older queued move a no-op instead of letting it run after Stop.
            if self._versions.get(camera.id) != version:
                return
            try:
                await onvif._soap_post(
                    client,
                    target.xaddr,
                    action=f"{PTZ_NS}/ContinuousMove",
                    body=body,
                    username=target.username,
                    password=target.password,
                )
            except onvif.OnvifError as exc:
                raise PtzError(str(exc)) from exc
        if self._versions.get(camera.id) != version:
            return
        self._cancel_dead_man(camera.id)
        self._dead_men[camera.id] = asyncio.create_task(
            self._dead_man(target, version), name=f"ptz-dead-man-{camera.id}"
        )

    async def _send_stop(self, target: _ControlTarget) -> None:
        client = await self._get_client()
        zoom_stop = "<Zoom>true</Zoom>" if target.zoom_supported else ""
        body = (
            f'<Stop xmlns="{PTZ_NS}">'
            f"<ProfileToken>{escape(target.profile_token)}</ProfileToken>"
            f"<PanTilt>true</PanTilt>{zoom_stop}"
            "</Stop>"
        )
        try:
            await onvif._soap_post(
                client,
                target.xaddr,
                action=f"{PTZ_NS}/Stop",
                body=body,
                username=target.username,
                password=target.password,
            )
        except onvif.OnvifError as exc:
            raise PtzError(str(exc)) from exc

    async def stop_camera(self, camera: Camera) -> None:
        target = self._target(camera)
        self._versions[camera.id] = self._versions.get(camera.id, 0) + 1
        self._cancel_dead_man(camera.id)
        lock = self._locks.setdefault(camera.id, asyncio.Lock())
        async with lock:
            await self._send_stop(target)
        self._schedule_patrol_resume(target, self._versions[camera.id])

    async def _dead_man(self, target: _ControlTarget, version: int) -> None:
        try:
            await asyncio.sleep(DEAD_MAN_SECONDS)
            if self._versions.get(target.camera_id) != version:
                return
            lock = self._locks.setdefault(target.camera_id, asyncio.Lock())
            async with lock:
                if self._versions.get(target.camera_id) != version:
                    return
                await self._send_stop(target)
            self._schedule_patrol_resume(target, version)
            logger.warning(
                "PTZ movement stopped by dead-man timer",
                extra={"camera_id": target.camera_id, "camera": target.camera_name},
            )
        except asyncio.CancelledError:
            raise
        except PtzError:
            logger.warning(
                "PTZ dead-man stop failed",
                extra={"camera_id": target.camera_id, "camera": target.camera_name},
                exc_info=True,
            )
        finally:
            if self._dead_men.get(target.camera_id) is asyncio.current_task():
                self._dead_men.pop(target.camera_id, None)


_manager: PtzManager | None = None


def get_manager() -> PtzManager:
    global _manager
    if _manager is None:
        _manager = PtzManager()
    return _manager


def set_manager(manager: PtzManager | None) -> None:
    """Replace the process singleton in isolated tests."""
    global _manager
    _manager = manager
